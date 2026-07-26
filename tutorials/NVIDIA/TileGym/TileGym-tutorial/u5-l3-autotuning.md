# Autotuning 机制

> 本讲属于 U5「分块 GEMM 与 Autotuning」的第三讲，承接 u5-l2 讲过的静态持久化调度与 `replace_hints` 结论，把视角从「单个内核怎么调度」抬到「整个内核族怎么自动选出最优配置」。前置认知：你已经知道 cuTile 内核由 `@ct.kernel` 定义、由 `ct.launch` 启动，`num_ctas`/`occupancy` 是编译器 hint、`TILE_SIZE_*`/`LOAD_LATENCY` 是 `ct.Constant` 内核参数（见 u5-l1、u5-l2）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 cuTile 的「tune-once / cache / launch」三段式自动调优流程——`exhaustive_search` 怎么搜、`grid_fn`/`args_fn`/`hints_fn` 三个回调各管什么、结果怎么缓存。
2. 读懂 `_matmul_autotune_configs()` 与 `_static_persistent_matmul_autotune_configs()`，理解它们为什么**按 GPU 架构（sm80 / sm100+ / sm120）产出不同的候选集**。
3. 解释**模块级 tune cache** 的键设计，以及为什么必须把 `(best_cfg, tuned_kernel)` 一起缓存、绝不能在热路径上反复 `replace_hints`。
4. 掌握全局开关 `TILEGYM_DISABLE_AUTOTUNE` 的契约（`is_autotune_enabled()` / `is_autotune_disabled()`），并准确说出**哪些代码真正读了它、哪些没有**。
5. 说清楚 `LOAD_LATENCY` 这个字段为什么**每条候选配置都必须带**，哪怕大多数架构都填 `-1`。

## 2. 前置知识

在进入源码前，先用直觉理解三件事。

**(A) 为什么需要自动调优？** 同一个矩阵乘 `C = A @ B`，输出瓦片切成 `128×128` 还是 `256×256`、一个 SM 上同时跑几个 CTA（occupancy）、要不要把多个 CTA 聚成簇（num_ctas/CGA）—— 这些选择在不同 GPU 架构、不同 `(M,N,K)` 形状上最优值都不同。手写「一个写死的最好配置」要么只在一台机器上快、换个架构就慢，要么得为每种情况写一堆 `if`。自动调优的思路是：**给定一个候选配置集合，在真实硬件上各跑一遍，挑最快的，然后记住它。**

**(B) 什么是「调」什么不是。** 这是最容易混淆的点，务必和 u5-l2 串起来：

| 量 | 类别 | 注入方式 | 本讲是否参与搜索 |
|---|---|---|---|
| `TILE_SIZE_M/N/K` | 内核参数 `ct.Constant[int]` | `ct.launch` 的 args | 是（每条候选不同） |
| `GROUP_SIZE_M`、`LOAD_LATENCY`、`TRANSPOSE_A/B` | 内核参数 `ct.Constant` | `ct.launch` 的 args | `LOAD_LATENCY` 形式上带、值多数为 `-1` |
| `num_ctas`、`occupancy` | **编译器 hint** | `@ct.kernel(...)` 或 `replace_hints(...)` | 是 |

也就是说，瓦片尺寸是「内核参数」、`num_ctas`/`occupancy` 是「编译器 hint」，两者都最终编译期固定，但**注入路径不同**。自动调优同时搜索这两类量，但写回时只把 hint 用 `replace_hints` 烤进内核，瓦片尺寸仍走 launch args（见 4.4）。

**(C) tune-once / cache / launch 是什么。** 第一次遇到某个 `(形状, dtype, 设备)` 时，花几秒到几十秒把候选集全跑一遍、选出最优；之后把「最优配置 + 烤好 hint 的内核对象」存进一个进程内的字典，后续每次调用都是一次普通 `ct.launch`，**零额外开销**。这个「只调一次、永久缓存、之后直接启动」的模式，是 cuTile 自动调优区别于 Triton `@triton.autotune` 装饰器的核心工程特征（见 4.4 对照）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/tilegym/autotune.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/autotune.py) | 全库唯一的自动调优策略开关：环境变量 `TILEGYM_DISABLE_AUTOTUNE` + 两个查询函数 `is_autotune_disabled()` / `is_autotune_enabled()`。业务代码只调函数、不读环境变量。 |
| [src/tilegym/ops/cutile/matmul.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py) | cuTile 版 matmul 全部实现。本讲主样本：两个候选生成函数、两个 `_cutile_autotune_*` 调优函数、模块级 tune cache，以及最外层 `matmul()` 入口。 |
| [src/tilegym/ops/cutile/utils.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/utils.py) | `cached_replace_hints`：一个全局 LRU，避免 `replace_hints` 在热路径上反复触发重编译。本讲用它解释「缓存整个内核对象」的替代写法。 |
| [tests/benchmark/bench_matrix_multiplication.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_matrix_multiplication.py) | matmul 非持久化路径的基准脚本（本讲实践的入口之一）。 |
| [tests/benchmark/bench_persistent_matmul.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_persistent_matmul.py) | matmul 持久化路径的基准脚本（带 `LOAD_LATENCY` 的那条路径）。 |

> 还有一类「同族用法」文件不在 `source_files` 里，但本讲会引用它们来证明开关的契约：`src/tilegym/suites/flashinfer/cutile/gemm/ragged_bmm.py`、`masked_bmm.py`、`rope_quantize_fp8.py` 等 suite 内核真正调用了 `is_autotune_enabled()`。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：① autotune 全局开关；② 按架构产出候选配置（含 `LOAD_LATENCY`）；③ `exhaustive_search` 调优流程；④ 模块级 tune cache 与 tune-once/cache/launch。

### 4.1 autotune 全局开关：autotune.py

#### 4.1.1 概念说明

`autotune.py` 只解决一个问题：**给整个进程一个唯一的「要不要自动调优」策略，并且只允许通过一个环境变量、两个函数来读它。** 它本身不做任何调优，只是一个「策略集中、读法唯一」的小模块（和 u2-l3 讲过的 `selector.py` 共享同一种工程哲学：策略集中在一处、业务代码绝不直接读环境变量）。

唯一的环境变量是 `TILEGYM_DISABLE_AUTOTUNE`：

- 不设 → 自动调优**开启**（默认）。
- 设为 `1/true/yes/on`（大小写、首尾空格不敏感）→ **关闭**自动调优。
- 设为 `0/false/no/off` → 保持开启。
- 设成别的乱码 → 直接抛 `ValueError`，而不是静默忽略。

#### 4.1.2 核心流程

```
业务代码想决策"要不要调优"
        │
        ▼
调用 is_autotune_enabled()   # 唯一入口
        │
        ▼
return not is_autotune_disabled()
        │
        ▼
读 os.environ["TILEGYM_DISABLE_AUTOTUNE"]
        │
   ┌────┴────────────────────┐
   ▼ 未设          ▼ 设了
return False     strip().lower() 后查表：
                 ① 命中"真值集合" → return True(禁用)
                 ② 命中"假值集合" → return False(启用)
                 ③ 都不命中      → raise ValueError
```

注意三个工程细节：① 读环境变量是**每次调用现读**（不缓存），所以进程运行中途改环境变量能即时生效；② 非法值「快速失败」而非吞掉；③ 这个开关控制的是**整个进程的策略**，不是某个内核、某个形状的开关。

#### 4.1.3 源码精读

常量与合法值集合（注意 `_DISABLE_AUTOTUNE_TRUE_VALUES` / `_FALSE_VALUES` 是 `frozenset`，查表 O(1) 且不可变）：

[src/tilegym/autotune.py:L5-L7](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/autotune.py#L5-L7) —— 定义环境变量名 `TILEGYM_DISABLE_AUTOTUNE` 与两组合法取值。

判定函数本体，注意「未设即启用」「strip+lower 归一化」「非法即报错」三段：

[src/tilegym/autotune.py:L10-L33](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/autotune.py#L10-L33) —— `is_autotune_disabled()`：进程级自动调优策略的唯一裁判。

对外更顺手的别名（业务侧多数调这个正向名字）：

[src/tilegym/autotune.py:L36-L38](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/autotune.py#L36-L38) —— `is_autotune_enabled()` 就是 `not is_autotune_disabled()`。

> ⚠️ **本讲最重要的准确性提醒**：本讲的主样本 `src/tilegym/ops/cutile/matmul.py` **并没有** import 这两个函数，它的两个 `_cutile_autotune_*` 函数**永远走自动调优**（靠 tune cache 把开销摊到只付一次）。真正在代码里 `if is_autotune_enabled(): ... else: 走固定配置` 的是 **suites** 里的内核，例如 `src/tilegym/suites/flashinfer/cutile/gemm/ragged_bmm.py`、`masked_bmm.py`、`rope_quantize_fp8.py`。所以「开关」是**全库契约**，但「是否在某个内核里接进开关」是逐内核的设计选择——这取决于该内核候选集有多大、固定配置够不够好。本讲的实践环节会专门验证这一点，避免你误以为 `TILEGYM_DISABLE_AUTOTUNE=1` 会让 matmul 变慢或跳过调优。

#### 4.1.4 代码实践

**目标**：验证开关的解析行为，且不依赖任何 GPU。

**操作步骤**（纯 CPU 即可，无需 CUDA）：

1. 写一段小脚本 `probe_switch.py`：
   ```python
   # 示例代码：仅演示开关解析，与内核无关
   import os, tilegym.autotune as A
   for v in [None, "1", "TRUE", " on ", "0", "nope"]:
       if v is None:
           os.environ.pop("TILEGYM_DISABLE_AUTOTUNE", None)
       else:
           os.environ["TILEGYM_DISABLE_AUTOTUNE"] = v
       try:
           print(repr(v), "-> enabled?", A.is_autotune_enabled())
       except ValueError as e:
           print(repr(v), "-> ValueError:", e)
   ```
2. 运行 `python probe_switch.py`。

**需要观察的现象**：`None/0` → `enabled? True`；`1/TRUE/ on ` → `enabled? False`；`"nope"` → 抛 `ValueError`，报错信息里列出全部合法值。

**预期结果**：与上一行完全一致。若 `tilegym.autotune` 因缺少 `cuda.tile` 而无法 import，说明你装的不是「核心库可用」环境——`autotune.py` 本身不依赖 `cuda.tile`，可单独把它复制出来测；其余情况标注「待本地验证」。

#### 4.1.5 小练习与答案

**Q1**：为什么 `is_autotune_disabled()` 每次都现读 `os.environ`，而不是在模块加载时读一次缓存？

**答**：为了让进程运行中途（例如测试用 `monkeypatch.setenv`）修改开关能即时生效；缓存一次会让「同进程内切策略」失效。代价只是几次字典查找，可忽略。

**Q2**：把开关设成 `"2"` 会怎样？为什么这样设计而不是默认当成「关闭」？

**答**：抛 `ValueError`。设计成「非法即报错」而非静默回退默认值，是为了避免用户拼错环境变量名（比如 `TILEGYM_DISABLE_AUTOTUNE=ture`）时，系统悄悄按「启用调优」跑，却让用户以为「关闭成功」——这种静默错误在性能调优场景里极难排查。

---

### 4.2 按架构产出候选配置

#### 4.2.1 概念说明

`exhaustive_search` 需要一个**候选配置集合**作为输入。cuTile 的候选不是「一个大笛卡尔积」，而是**先按 GPU 架构分流，再为每个架构手工挑出少量高质量候选**。这么做有两个原因：

1. **cuTile 单次编译很重**（约 0.5–1s/配置），盲目展开几十上百个配置会让首次调优等很久；
2. 不同架构的「最优瓦片形状 / 最大 num_ctas」差异巨大（A100 没有 CGA、Blackwell 有、sm120 又是另一档），统一搜纯属浪费。

所以两个生成函数都用 `torch.cuda.get_device_capability()` 拿到 `(主版本, 次版本)`，走三分支 `if/elif/else` 产出该架构专属的候选。

#### 4.2.2 核心流程

`_matmul_autotune_configs()`（非持久化路径）的三分支：

```
读 torch.cuda.get_device_capability()
        │
   ┌────┼──────────────────┐
   ▼    ▼                  ▼
(12,0)/(12,1)      [0]<9            else
sm120/sm121        sm80(A100)       sm100+(Blackwell)
2 个候选           TM,N,K∈        4 个大瓦片候选
(128,64,64/32)    {64,128}×       含 num_ctas=2/4
                  {64,128}×
                  {32,64,128}×
                  occ∈{1,2}  → 24 候选
```

非持久化 grid 只与输出瓦片数有关（一块算一个输出瓦片）：

\[ \text{grid} = \left\lceil \frac{M}{T_M} \right\rceil \cdot \left\lceil \frac{N}{T_N} \right\rceil \]

`_static_persistent_matmul_autotune_configs()`（持久化路径，带 `LOAD_LATENCY`）也是三分支，但每条候选多一个 `GROUP_SIZE_M` 与 `LOAD_LATENCY` 字段；其 grid 走持久化公式：

\[ \text{grid} = \min\!\left(\left\lfloor \frac{\text{NUM\_SM}}{n_{\text{ctas}}} \right\rfloor,\ \left\lceil \frac{M}{T_M} \right\rceil \left\lceil \frac{N}{T_N} \right\rceil \right) \cdot \text{occupancy} \]

#### 4.2.3 源码精读

非持久化候选，注意 `SimpleNamespace` 当配置对象、`num_ctas` 在 pre-SM90 恒为 1（A100 无 CGA）：

[src/tilegym/ops/cutile/matmul.py:L47-L72](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L47-L72) —— `_matmul_autotune_configs()`：按 sm120 / pre-SM90 / sm100+ 三档产出候选，是典型的「按架构裁剪搜索空间」。

持久化候选，开头注释解释了 `LOAD_LATENCY` 的取值含义与「为何每条都得带」：

[src/tilegym/ops/cutile/matmul.py:L74-L83](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L74-L83) —— 注释点明：`LOAD_LATENCY` 是 `ct.load` 的代价提示（1..10，`-1`=编译器推断）；目前只有 sm90 会真正调它，其余架构都填 `-1`，**但每条配置都必须带这个字段**，因为内核无条件读取 `cfg.LOAD_LATENCY`。

[src/tilegym/ops/cutile/matmul.py:L83-L137](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L83-L137) —— sm120 / sm80 / sm100+ 三档持久化候选，每条都带 `GROUP_SIZE_M=8` 与 `LOAD_LATENCY=-1`。

最外层 `matmul()` 入口，按 `static_persistent` 把请求分流到两条调优函数之一：

[src/tilegym/ops/cutile/matmul.py:L416-L450](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L416-L450) —— `@register_impl("matmul", backend="cutile")` 的 `matmul()`：算出 M/N/K、分配输出 `c`、按 `static_persistent` 选 `_cutile_autotune_static_persistent_matmul` 或 `_cutile_autotune_matmul`。注意非持久化分支还会 `assert trans_a/trans_b == False`。

> **关于 `LOAD_LATENCY` 的完整答案**（本讲实践题之一）：① 它是内核签名里的 `ct.Constant[int]` 参数（见 [matmul.py:L227](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L227)），`args_fn` 对**每条**候选都无条件传入 `cfg.LOAD_LATENCY`（见 4.3）；哪条候选缺这个字段，`exhaustive_search` 调它时就会 `AttributeError`。② 值 `<=0` 时内核走「编译器推断」分支，**省略** `ct.load` 的 `latency=` 关键字（因为 `ct.load` 不接受 `-1`）；`>=1` 时才真正把代价提示喂给两个操作数的 load（见 [matmul.py:L249-L307](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L249-L307) 的 `if LOAD_LATENCY >= 1: ... else: ...` 显式分支——cuTile tracer 不支持 `**kwargs` 解包，所以必须写成编译期 `if/else`）。③ 它是 `Constant`，每个不同值都会特化出不同内核，所以「带字段」与「真正调优它」是两件事：目前多数架构带的是 `-1`（=不调），只是为了让签名闭合。

#### 4.2.4 代码实践

**目标**：在不跑内核的前提下，观察「同一段代码在不同架构产出不同候选集」。

**操作步骤**：

1. 阅读上面引用的 [matmul.py:L47-L72](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L47-L72) 与 [matmul.py:L83-L137](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L83-L137)。
2. 做一张表：列出 sm80 / sm100+ / sm120 三档下，非持久化与持久化各自的候选数、最大 `num_ctas`、`LOAD_LATENCY` 取值。
3. （可选，需 GPU）在 Python 里 `print(torch.cuda.get_device_capability())`，确认本机走哪一档。

**需要观察的现象**：pre-SM90 段 `num_ctas` 恒为 1（无 CGA）；sm100+ 才出现 `num_ctas=2/4`；sm120 的瓦片明显比 sm80 小（64×64 居多）。

**预期结果**：表格能反映「架构越新、瓦片越大、可用 num_ctas 越多」的总体趋势。无 GPU 时填「待本地验证」。

#### 4.2.5 小练习与答案

**Q1**：为什么 pre-SM90（A100）分支把 `num_ctas` 写死成 1，而不是也搜一搜？

**答**：CGA（线程块簇）是 sm90+ 才有的硬件特性，pre-SM90 上 `num_ctas>1` 无意义甚至不可用；写死 1 既正确又省编译时间。这正体现了「按架构裁剪」的价值：不在不可能赢的维度上浪费搜索。

**Q2**：`_matmul_autotune_configs()` 里 sm100+ 只给了 4 条候选，而 pre-SM90 给了 24 条。为什么 Blackwell 反而更少？

**答**：Blackwell 的张量核心与内存子系统相对更「可预测」，少量大瓦片配置就能覆盖大部分形状；而 A100 上瓦片/occupancy 的最优组合对形状更敏感，需要更细的网格搜索。候选数是「够用就好」的工程权衡，不是越多越好。

---

### 4.3 exhaustive_search 调优流程

#### 4.3.1 概念说明

`exhaustive_search` 来自 `cuda.tile.tune`，是 cuTile 自动调优的**核心引擎**：给它一组候选、一个流、一个算 grid 的回调、一个内核、一个算 args 的回调、一个算 hint 的回调，它就会**逐个候选编译并实测、返回 `TuningResult`**，其中 `result.best.config` 是最快的那个 `SimpleNamespace`。

关键设计：grid、args、hint **分别由三个 lambda 提供**，而不是混在一起。原因有二：

- **grid 随候选变**：瓦片越大，输出瓦片数越少，grid 越小；持久化路径还要乘 occupancy、除 num_ctas。
- **args 随候选变**：`TILE_SIZE_*`、`LOAD_LATENCY` 要按候选填进内核的 ConstInt 形参；而 hint（`num_ctas`/`occupancy`）走另一条路（`hints_fn`），最终由 `replace_hints` 烤进内核，**不进 args**。

此外，整个搜索被 `ct.compiler_timeout(5)` 包住，给每个候选的编译设了 5 秒上限，防止单条配置卡死整个调优（这是 skills 文档里反复强调的「编译超时」坑）。

#### 4.3.2 核心流程

非持久化路径 `_cutile_autotune_matmul` 的调优阶段：

```
cache_key = (M, N, K, dtype, str(device))
        │
   命中缓存？── 是 ──→ 跳到「直接启动」
        │ 否
        ▼
with ct.compiler_timeout(5):
    exhaustive_search(
        configs   = list(_matmul_autotune_configs()),
        stream,
        grid_fn   = λcfg: (⌈M/TM⌉·⌈N/TN⌉, 1, 1),
        kernel    = _matmul_kernel,
        args_fn   = λcfg: (a, b, c, TM, TN, TK),
        hints_fn  = λcfg: {"num_ctas": cfg.num_ctas,
                           "occupancy": cfg.occupancy},
    )
        │
        ▼
best_cfg = result.best.config
缓存 (best_cfg, _matmul_kernel.replace_hints(num_ctas=..., occupancy=...))
```

持久化路径结构相同，只是 `cache_key` 多了 `trans_a/trans_b`、`grid_fn` 换成持久化公式、`args_fn` 多塞 `M,N,K,trans_a,trans_b,GROUP_SIZE_M,LOAD_LATENCY`。

#### 4.3.3 源码精读

模块顶部导入调优引擎，并声明两个**模块级 tune cache**：

[matmul.py:L10-L17](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L10-L17) —— `from cuda.tile.tune import exhaustive_search` 与两条 cache 字典，注释写明键的语义。

非持久化调优函数，注意三个 lambda 的分工与 `compiler_timeout(5)`：

[matmul.py:L322-L348](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L322-L348) —— `_cutile_autotune_matmul`：先查缓存；未命中则 `exhaustive_search` 三回调齐发，把 `(best_cfg, replace_hints 后的内核)` 存进 cache；最后用缓存里的 `tuned_kernel` 直接 `ct.launch`。

持久化调优函数，对比看 `grid_fn` / `args_fn` 的差异：

[matmul.py:L351-L413](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L351-L413) —— `_cutile_autotune_static_persistent_matmul`：grid 用持久化公式（`min(NUM_SMS//num_ctas, 输出瓦片数) * occupancy`），`args_fn` 把 `LOAD_LATENCY` 等 ConstInt 一并传入；`hints_fn` 与非持久化完全一样（都只回 `num_ctas`/`occupancy`）。

三个 lambda 的对照表（本讲核心结论之一）：

| 回调 | 非持久化 | 持久化 | 作用 |
|---|---|---|---|
| `grid_fn` | `(⌈M/TM⌉·⌈N/TN⌉,1,1)` | `(min(NUM_SM//nc, ⌈M/TM⌉⌈N/TN⌉)·occ,1,1)` | 每条候选的 grid 不同 |
| `args_fn` | `(a,b,c,TM,TN,TK)` | `(a,b,c,M,N,K,TM,TN,TK,ta,tb,GSM,LOAD_LATENCY)` | 填内核 ConstInt 形参 |
| `hints_fn` | `{num_ctas, occupancy}` | `{num_ctas, occupancy}` | 编译器 hint，**不进 args** |

#### 4.3.4 代码实践

**目标**：读懂「同一内核、不同 grid/args」是怎么由候选驱动的。

**操作步骤**：

1. 打开 [matmul.py:L322-L348](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L322-L348)。
2. 对 `_matmul_autotune_configs()` 在 sm100+ 产出的 4 条候选，**手算**每条的 grid（设 `M=N=4096`）。例如 `TILE_SIZE_M=N=128` 那条：grid = `(⌈4096/128⌉², 1, 1) = (1024, 1, 1)`；`256×256` 那条：grid = `(256, 1, 1)`。
3. 思考：grid 差这么多，`exhaustive_search` 是怎么「公平」比较它们的？

**需要观察的现象**：瓦片越大，grid 越小、单 CTA 干的活越多；`exhaustive_search` 比的是**整轮启动的实际耗时**（含编译后的实测），而非「grid 大小」，所以大小瓦片能同台竞技。

**预期结果**：手算的 grid 与上表公式一致；关于「公平性」的解释——`exhaustive_search` 对每条候选都真实启动并计时，挑 wall-clock 最短者，因此 grid 大小本身不是评判标准。无法在本机实测时标「待本地验证」。

#### 4.3.5 小练习与答案

**Q1**：为什么 `hints_fn` 只返回 `num_ctas`/`occupancy`，而不把 `TILE_SIZE_*` 也塞进去？

**答**：因为 `num_ctas`/`occupancy` 是**编译器 hint**（通过 `replace_hints` 注入），而 `TILE_SIZE_*` 是**内核参数**（`ct.Constant[int]`，通过 `ct.launch` 的 args 注入、由 JIT 缓存按值特化）。两者注入路径不同，`hints_fn` 只管 hint 这一栏。

**Q2**：把 `ct.compiler_timeout(5)` 去掉会有什么风险？

**答**：某条候选若触发了 cuTile 编译器的病态路径（大瓦片 + 高 occupancy 容易触发），可能卡几十秒甚至更久，导致整个首次调优体验极差。5 秒上限是「单候选编译」的护栏，不是「整轮搜索」的总时限。

---

### 4.4 模块级 tune cache 与 tune-once/cache/launch

#### 4.4.1 概念说明

调优很贵（编译 + 实测），但**同一个 `(形状, dtype, 设备)` 的最优配置是稳定的**——同一台机器、同一个形状，今天最快的就是明天最快的。所以 cuTile 的标准模式是 **tune-once / cache / launch**：

1. **tune-once**：首次遇到某 cache_key，跑一遍 `exhaustive_search`；
2. **cache**：把 `(best_cfg, tuned_kernel)` 存进**模块级字典**（随模块生命周期存活，进程内有效）；
3. **launch**：之后命中缓存，直接用 `tuned_kernel` 走一次普通 `ct.launch`，零额外开销。

这里有一个**必须牢记的坑**：缓存里存的不只是 `best_cfg`，还要存 `tuned_kernel = kernel.replace_hints(num_ctas=..., occupancy=...)`。原因是 `replace_hints` 会生成一个**带独立 JIT 缓存的新内核对象**——如果每次调用都现 `replace_hints`，等于每次都触发重编译（慢 100–500 倍）。把它和配置一起缓存，就把「重编译」摊到只付一次。

> 对照 Triton：Triton 用 `@triton.autotune` 装饰器 + `Config(...)` 对象，cache 由运行时自动管理；cuTile 没有 `num_warps`/`num_stages`（由编译器决定），只有瓦片尺寸 + `occupancy` + `num_ctas`，cache 是**用户自管理**的进程内字典（无持久化），并把 `args_fn`（内核参数）与 `hints_fn`（编译器 hint）显式分开。

#### 4.4.2 核心流程

两条 cache 的键与值：

| cache | 键 (cache_key) | 值 |
|---|---|---|
| `_matmul_tune_cache` | `(M, N, K, dtype, str(device))` | `(best_cfg, tuned_kernel)` |
| `_static_persistent_matmul_tune_cache` | `(M, N, K, trans_a, trans_b, dtype, str(device))` | `(best_cfg, tuned_kernel)` |

注意持久化路径的键多了 `trans_a/trans_b`——因为持久化内核支持转置（见 u5-l2），转置与否会改变最优配置，必须区分。

命中与否的分支：

```
if cache_key 不在 cache:
    调 exhaustive_search … 得 best_cfg
    cache[cache_key] = (best_cfg,
                        kernel.replace_hints(num_ctas=…, occupancy=…))
best_cfg, tuned_kernel = cache[cache_key]   # 必然命中
ct.launch(stream, grid, tuned_kernel, args)  # 普通启动
```

#### 4.4.3 源码精读

「未命中→搜→存」与「命中→直接 launch」的完整闭环，非持久化：

[matmul.py:L326-L348](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L326-L348) —— 注意 `replace_hints` 只在「未命中」分支里调一次，之后永远用缓存的 `tuned_kernel`；`ct.launch` 用的是 `best_cfg.TILE_SIZE_*` 作为 ConstInt args（瓦片特化发生在 launch 时，不在 replace_hints 里）。

持久化的对应闭环：

[matmul.py:L354-L413](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L354-L413) —— 同样的模式；注意 launch 的 args 里把 `best_cfg.LOAD_LATENCY` 也一并传入，使瓦片尺寸与 LOAD_LATENCY 的特化都在这次 launch 里完成。

另一种写法：suite 内核常用 `cached_replace_hints`（一个按 `(id(kernel), hints)` 去重的全局 LRU），用于「不按形状缓存、只按内核+hint 缓存」的场景：

[src/tilegym/ops/cutile/utils.py:L11-L33](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/utils.py#L11-L33) —— `cached_replace_hints`：用 `OrderedDict` 做容量 256 的 LRU，并额外持有 `owner=kernel` 防止源内核被回收后 `id()` 被复用（否则缓存键会撞车）。本讲的 matmul 没用它（自己按形状缓存了整对象），但 suite 的 `ragged_bmm`/`masked_bmm`/`gemm_alpha_beta` 都用它。

> **一句话总结 cache 设计**：缓存的对象粒度有两种——①「按形状缓存整个 tuned_kernel」（matmul 的做法，最优，因为连瓦片特化都一起缓存了）；②「按 `(kernel, hint)` 缓存 hinted 内核」（`cached_replace_hints`，适合不调瓦片、只调 occupancy 的内核）。两者都是为了把 `replace_hints`/重编译挡在热路径之外。

#### 4.4.4 代码实践

**目标**：用基准脚本观察「冷缓存（首次调优）」与「热缓存（直接启动）」的耗时差。

**操作步骤**：

1. 确认已装好 tilegym 并能 `import cuda.tile`（见 u1-l2）。在 `tests/benchmark/` 下：
   ```bash
   # 示例命令：非持久化 matmul 基准，会触发 _cutile_autotune_matmul
   python tests/benchmark/bench_matrix_multiplication.py
   # 持久化路径（带 LOAD_LATENCY 的那条）
   python tests/benchmark/bench_persistent_matmul.py
   ```
2. 关注**第一个形状**的首次计时（冷：含编译+搜索）与其后相同形状的计时（热：仅 launch）。

**需要观察的现象**：第一个 `(M,N,K)` 会停顿几秒到几十秒（编译 N 条候选 + 实测），之后相同形状几乎瞬时；不同形状各自有独立的「首次停顿」。

**预期结果**：冷/热耗时差距可达数十倍，正说明 tune-once/cache 的价值。**注意**：此实践**不受** `TILEGYM_DISABLE_AUTOTUNE=1` 影响——本讲的 matmul 路径不读这个开关（见 4.1.3 的提醒）。无 GPU 时标「待本地验证」。

#### 4.4.5 小练习与答案

**Q1**：如果删掉 cache，让每次 `matmul()` 都现跑 `exhaustive_search`，会怎样？

**答**：每次调用都要重新编译全部候选（每条 0.5–1s）并实测，matmul 会从「微秒级」退化到「秒级」，完全不可用。这就是 skills 文档里「Pitfall #7：在热路径上 `replace_hints`」要防的——cache 正是它的解药。

**Q2**：持久化路径的 cache_key 为什么比非持久化多 `trans_a/trans_b`，而非持久化入口还要 `assert trans_a/trans_b == False`？

**答**：因为 cuTile 的 matmul **只有持久化路径支持转置**（u5-l2 已讲）。非持久化内核根本不接收转置参数，入口直接 assert 拒绝；持久化内核支持转置，且转置会改变访存模式与最优配置，所以必须进 cache_key 区分。

**Q3**：`cached_replace_hints` 为什么要额外保存 `owner=kernel`？

**答**：它的键用 `id(kernel)`。Python 回收源内核后，`id()` 可能被新对象复用，导致键撞车、返回错误的缓存内核。持有 `owner` 引用可防止源内核被提前回收。

---

## 5. 综合实践

把本讲四块知识串成一个端到端的小任务。

**任务**：给一个假想的「新增 cuTile 算子」规划自动调优，并解释每个设计选择。假设你要给 u4-l1 的 `silu_and_mul`（逐元素、行级、不跨元素归约）加自动调优。

1. **该搜哪些维度？** 参考 4.2 的决策树思路：逐元素内核是访存受限的，`num_ctas` 与瓦片尺寸基本无收益，唯一有效旋钮是 `occupancy`。请写出候选生成器骨架（`SimpleNamespace(occupancy=occ) for occ in [1,2,4,8]`）。
2. **三个回调怎么写？**
   - `grid_fn`：`λcfg: (min(NUM_SM*cfg.occupancy, N_ROWS), 1, 1)`（grid-stride，回顾 u3-l1/u3-l3）；
   - `args_fn`：`λcfg: (a, b, out, ...)`（逐元素内核通常没有瓦片尺寸 ConstInt）；
   - `hints_fn`：`λcfg: {"occupancy": cfg.occupancy}`（只回 occupancy）。
3. **cache_key 怎么设计？** 至少 `(N_ROWS, H, dtype, str(device))`；要不要进 `trans`？逐元素无转置，不需要。
4. **要不要接 `TILEGYM_DISABLE_AUTOTUNE` 开关？** 参照 suite 内核：写 `if is_autotune_enabled(): 走调优 else: 走固定 occupancy` 的分支，并准备一个「固定 occupancy」的回退配置。
5. **LOAD_LATENCY 呢？** 逐元素内核若不在签名里声明 `LOAD_LATENCY: ct.Constant[int]`，就不需要带这个字段——它只是 matmul 持久化内核的专属需求（4.2.3）。

**交付物**：一段约 30 行的 `_cutile_autotune_silu_and_mul(stream, a, b, out)` 伪代码（标注「示例代码」），覆盖 tune-once/cache/launch 三段，并在注释里写明「为何 occupancy-only」「为何 cache_key 这样选」。完成后与 `tests/benchmark/bench_silu_and_mul.py` 的真实写法对照（若该算子已接入调优），修正你的假设。

> 这个综合实践不需要你真的改源码（本讲禁止改源码），重点是让你把「候选生成 → exhaustive_search → cache → 开关」四件事在一个新算子上重新推演一遍。

## 6. 本讲小结

- cuTile 自动调优 = **tune-once / cache / launch**：首次按形状跑 `exhaustive_search` 选最优，之后命中模块级 cache 直接 `ct.launch`，零额外开销。
- 候选配置**按 GPU 架构分流**（sm120 / pre-SM90 / sm100+），不同架构给不同瓦片与 `num_ctas` 上限，避免在无效维度上浪费编译时间。
- `exhaustive_search` 用三个回调 `grid_fn`/`args_fn`/`hints_fn` 解耦「grid、内核参数、编译器 hint」；只有 `num_ctas`/`occupancy` 进 `hints_fn` → `replace_hints`，瓦片尺寸走 `args_fn` → `ct.launch`。
- cache 必须把 `(best_cfg, tuned_kernel)` **整对象**缓存，绝不能在热路径上反复 `replace_hints`（否则每次重编译）；键设计要把所有影响最优配置的量（形状、dtype、设备、转置）都纳入。
- `TILEGYM_DISABLE_AUTOTUNE` 是**全库唯一**的自动调优开关（`is_autotune_enabled()`/`is_autotune_disabled()`），但**是否接入开关是逐内核的选择**——本讲的 matmul 不接（永远调优、靠 cache 摊销），suite 的 ragged_bmm/masked_bmm/rope_quantize_fp8 才接。
- `LOAD_LATENCY` 是持久化 matmul 内核的 `ct.Constant` 形参，因 `args_fn` 无条件读取，**每条候选都必须带**；多数架构填 `-1`（编译器推断），目前真正调它的只有 sm90。

## 7. 下一步学习建议

- **横向看其它内核的调优写法**：`src/tilegym/ops/cutile/group_gemm.py`（批量 GEMM，occupancy-only 调优）、`src/tilegym/ops/cutile/attention.py`（FMHA，瓦片 + num_ctas 全量搜索），对照本讲的「三类回调 + cache」模板，体会「按内核类型裁剪搜索维度」。
- **深入 suite 的开关分支**：读 `src/tilegym/suites/flashinfer/cutile/gemm/ragged_bmm.py` 里 `if is_autotune_enabled(): ... else: 走固定配置` 的完整写法，理解「调优路径」与「固定配置回退路径」如何共存。
- **下一讲 U6 进入注意力内核族**：u6-l1 的 FMHA 会复用本讲的 `exhaustive_search` + cache 模式，但搜索空间更复杂（ TILE_M × num_ctas 等），是检验你是否真懂本讲的好样本。
- **若要自己加调优**：直接读 `skills/tilegym-cutile-autotuning/SKILL.md` 及其 `references/`（按内核类型 T1–T9 给模板、列了 7 个常见坑），它是本讲所述机制的操作手册。
