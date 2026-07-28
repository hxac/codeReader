# GPU Profile 与 func 公式实现

> 本讲属于 Roofline 性能模型单元（u7）的第三讲。前置：u7-l1（SOL 模型与度量）、u7-l2（roofline 字段与 inline / func 两种模式）。

## 1. 本讲目标

学完本讲后，你应该能够：

1. 读懂 `tileops/perf/profiles/*.yaml` 的三段结构（`theoretical` / `calibration` / `effective`），并解释为什么 `effective = theoretical × calibration` 只在加载时计算、而不写进 YAML。
2. 会调用 `load_profile()` 拿到一个带 `effective` 字段的字典，并理解 M6（硬件标定）到 M5（roofline 工具）的数据契约接口。
3. 掌握 `func` 模式公式的**推荐签名** `func(op) -> (flops, bytes)`，知道 codegen 如何把 `roofline.func` 编译成 `eval_roofline` 的方法体。
4. 能看懂 `tileops/perf/formulas.py` 里「多输入 dtype 字节记账」与「逻辑算子融合计数」两类典型写法（`where` / `masked_fill` / `clamp` / `gemm_fp8` / 比较算子 / Mamba bias 变体），理解它们为什么必须用 func 模式而非 inline 模式。
5. 理解「基准与 roofline 解耦」：M4 只产 raw time + `(flops, bytes)`，M5 读数算效率，二者永不耦合。

## 2. 前置知识

### 2.1 SOL 效率复习（承接 u7-l1）

TileOPs 不和某个 baseline 比性能，而是和**硬件理论上限**比。Speed-of-Light（SOL）效率的定义是：

\[
\text{efficiency} = \frac{\text{sol\_time}}{\text{actual\_time}}
\]

其中 `sol_time` 是「这个 workload 在理想情况下最少要花的时间」：

\[
\text{sol\_time} = \max\!\left(\frac{\text{flops}}{\text{effective\_compute}},\ \frac{\text{bytes}}{\text{effective\_bandwidth}}\right)
\]

可以看到，算一次效率需要三类输入：

| 量 | 来源 | 本讲是否涉及 |
| --- | --- | --- |
| `flops`、`bytes`（workload 的算力/访存量） | manifest roofline，经 `eval_roofline()` 求值 | **是（func 公式）** |
| `effective_compute`、`effective_bandwidth`（硬件可达峰值） | GPU profile YAML | **是（profile）** |
| `actual_time`（实测耗时） | 基准 M4（`bench_kernel`） | 否（u6-l1） |

本讲把前两块拼齐：`formulas.py` 负责把 workload 翻译成 `(flops, bytes)`，`profile.py` 负责给出硬件的 effective 峰值。两块在 M5（roofline 工具）汇合算效率。

### 2.2 两个术语

- **theoretical peak（理论峰值）**：厂商 spec sheet 上的数字，例如 H200 的 HBM 带宽 4800 GB/s、fp16 tensor core 989.5 TFLOPS。这是「物理上绝不可能超过」的天花板，但也不是「日常能跑到的」。
- **calibration（标定系数）**：用一次性微基准（microbenchmark）测出来的、真实 kernel 能达到的理论峰值比例。例如 H200 的 HBM calibration=0.848，来自 STREAM Triad 测试；fp16 tensor core calibration=0.75，来自 cuBLAS 的 GEMM 峰值。

两者相乘得到 **effective peak（有效峰值）**，这才是 SOL 效率公式里当分母用的「现实天花板」。

### 2.3 inline 模式 vs func 模式复习（承接 u7-l2）

manifest 的 `roofline` 块有两种写法：

- **inline 模式**：直接写 `flops` / `bytes` 两个表达式字符串，codegen 把它们编译成纯 Python。表达式只能引用 vars 层解析出的局部变量、`elem_bytes`、以及白名单 helper。它把 `elem_bytes` 绑死在**单一 dtype** 上。
- **func 模式**：写 `func: "tileops.perf.formulas.xxx_roofline"`，指向一个人写的 Python 函数，推荐签名 `func(op) -> (flops, bytes)`。函数体里可以写任意 Python：条件分支、多 dtype 字节相加、`broadcast_shapes` 等。

本讲专注 func 模式。**何时必须用 func** 是贯穿全讲的主线：只要字节记账里出现了「不同 dtype 的输入混在一起」「需要 post-broadcast 的 `N_total`」「条件性张量存在」等情况，inline 模式就表达不了，必须 func。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| [tileops/perf/profile.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/profile.py) | GPU profile 加载器：读 YAML、做数字强转、注入 `effective`。M6→M5 数据契约接口。 | 模块 4.1、4.2 |
| [tileops/perf/profiles/h200.yaml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/profiles/h200.yaml) | NVIDIA H200 的 theoretical + calibration。 | 模块 4.1、4.2 |
| [tileops/perf/profiles/h20_3e.yaml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/profiles/h20_3e.yaml) | H20-3e 出口合规 SKU，用来对比 calibration 为何「结构性地偏高」。 | 模块 4.2 |
| [tileops/perf/formulas.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py) | func 模式公式集合，`func(op) -> (flops, bytes)`。本讲剖析的代表函数。 | 模块 4.3 |
| [tileops/ops/_roofline_codegen.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_roofline_codegen.py) | func 模式的 codegen：把 `roofline.func` 编译成 `eval_roofline` 方法体。 | 模块 4.3 |
| [benchmarks/benchmark_base.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py) | `ManifestBenchmark`：基准如何通过 `op.eval_roofline()` 拿 `(flops, bytes)`。 | 模块 4.3、综合实践 |

---

## 4. 核心概念与源码讲解

本讲三个最小模块：**4.1 profile 加载**、**4.2 effective 计算**、**4.3 func 公式签名**。

### 4.1 profile 加载

#### 4.1.1 概念说明

GPU profile 回答一个问题：「这台机器的 HBM 带宽和 tensor core 算力，现实里能跑到多少？」答案不是 spec sheet 上的一个数字，而是「理论峰值 × 标定系数」。

TileOPs 把这个答案拆成两个层次存进 YAML：

- **theoretical**：来自厂商 datasheet，永不改动。
- **calibration**：来自 `benchmarks/hardware/` 下的一次性微基准（HBM 用 STREAM Triad，tensor core 用 cuBLAS 峰值）。换硬件、换驱动时才需要重测。

第三层 **effective** 不写进 YAML，而是加载时算出来。这样做有两个好处：一是 YAML 是「可复现的原始测量」，不会被算出来的派生量污染；二是哪天你改了 calibration 的定义，effective 会自动跟着更新，不需要手改一堆数字。

`load_profile(gpu_name)` 就是这个加载器，它读 YAML、强转数字、注入 effective，返回一个普通 dict。它是模块 M6（HW Calibration，产出 profile）到 M5（roofline 工具，消费 profile）的唯一接口——见 profile.py 的模块 docstring 明说：

> This is the M6 -> M5 data contract interface (see docs/design/architecture.md).

#### 4.1.2 核心流程

`load_profile("h200")` 的执行过程：

```text
get_profile_path("h200")
   └─ 拼出 profiles/h200.yaml；不存在就 FileNotFoundError（列出可用 profile）
open + yaml.safe_load
   └─ 得到原始 dict（theoretical/calibration 都是字符串，如 "4800e9"）
_coerce_numeric_strings(data)
   └─ 递归把 key ∈ {theoretical, calibration, effective} 的字符串值转成 float
_inject_effective(data)
   └─ 对 hbm 段：data["hbm"]["effective"] = theoretical * calibration
   └─ 对 tensor_core 下每个 dtype 段（fp16/bf16/tf32/fp8）：同样注入 effective
return data
```

一个关键细节：YAML 里写 `4800e9`，PyYAML 会把它当成**字符串**（科学计数法不是 YAML 原生 float 语法）。所以必须有一步显式强转，但又不能把 `compute_capability: "9.0"` 这种本来就该是字符串的字段也转了。`_coerce_numeric_strings` 的做法是**只转特定 key 下的值**，而不是「凡是能 parse 成 float 的就转」。

#### 4.1.3 源码精读

**入口与路径解析**——找不到 profile 时会列出所有可用名字，方便排错：

[profile.py:71-86 — load_profile：读 YAML、强转、注入 effective](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/profile.py#L71-L86)

```python
def load_profile(gpu_name: str) -> dict:
    path = get_profile_path(gpu_name)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data = _coerce_numeric_strings(data)
    _inject_effective(data)
    return data
```

**强转策略**——`_NUMERIC_KEYS` 是一个冻结集合，只这三个 key 的值会被转 float，`compute_capability`、`gpu` 这类字段原样保留：

[profile.py:42-57 — _coerce_numeric_strings：按 key 名白名单递归强转](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/profile.py#L42-L57)

```python
_NUMERIC_KEYS = frozenset({"theoretical", "calibration", "effective"})

def _coerce_numeric_strings(obj, key=None):
    if isinstance(obj, dict):
        return {k: _coerce_numeric_strings(v, key=k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_numeric_strings(v) for v in obj]
    if isinstance(obj, str) and key in _NUMERIC_KEYS:
        try:
            return float(obj)
        except ValueError:
            return obj
    return obj
```

注意递归时把当前 key 往下传（`key=k`），叶子节点判断 `key in _NUMERIC_KEYS`——这样嵌套在 `tensor_core.fp16.theoretical` 里的值也能被正确识别。

**H200 YAML 长什么样**——只存 theoretical 和 calibration，注释写明每个 calibration 的测量来源（这是可审计性的关键）：

[h200.yaml:11-27 — hbm 与 tensor_core 段，calibration 注明来源](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/profiles/h200.yaml#L11-L27)

```yaml
hbm:
  theoretical: 4800e9        # bytes/s, spec sheet
  calibration: 0.848         # STREAM Triad, from benchmarks/hardware/memory/hbm_bandwidth.py

tensor_core:
  fp16:
    theoretical: 989.5e12    # FLOPS, spec sheet (dense)
    calibration: 0.75        # from benchmarks/hardware/compute/gemm_throughput.py (cuBLAS peak)
```

`tf32` 和 `fp8` 的 calibration 都标了 `placeholder — not yet measured`，说明这两档还没真正微基准过，沿用了 fp16 的 0.75 占位。这种「明示未测量」的注释是信任模型的一部分。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次 profile 加载，观察 YAML 字符串如何变成带 effective 的 float。

**操作步骤**（无 GPU 也能跑，只读 YAML）：

```python
# 示例代码：在仓库根目录 python -c 或写个小脚本
from tileops.perf.profile import load_profile

p = load_profile("h200")
print(p["gpu"], p["compute_capability"])
print("hbm theoretical:", p["hbm"]["theoretical"])
print("hbm calibration:", p["hbm"]["calibration"])
print("hbm effective  :", p["hbm"]["effective"])
print("fp16 effective :", p["tensor_core"]["fp16"]["effective"])
```

**需要观察的现象**：

- `theoretical` / `calibration` / `effective` 都是 `float`，而不是字符串 `"4800e9"`。
- `compute_capability` 仍是字符串 `"9.0"`（没被强转，因为它不在 `_NUMERIC_KEYS` 里）。
- `hbm["effective"]` 在 YAML 里根本不存在，是 `_inject_effective` 现场算出来的。

**预期结果**（待本地验证具体浮点表示）：

- `hbm["effective"] == 4800e9 * 0.848`，即约 4.0704e12 bytes/s。
- `tensor_core["fp16"]["effective"] == 989.5e12 * 0.75`，即约 742.125e12 FLOPS。

> ⚠️ 若本机未 `make install`，`from tileops.perf...` 可能因依赖缺失失败；该实践只依赖 `pyyaml`，可也直接 `open(profiles/h200.yaml)` + `yaml.safe_load` 复现核心逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `4800e9` 在 YAML 里是字符串，而 `0.848` 是 float？

> **答案**：YAML 1.1 的 float 语法不包含科学计数法的 `e` 记法（或说 PyYAML 的 resolver 对 `4800e9` 这种串不识别为 float，识别成普通字符串）。而 `0.848` 是合法的 YAML float。这就是 `_coerce_numeric_strings` 必须存在的根本原因——它兜底把 scientific-notation 字符串转回 float。

**练习 2**：假如你想加一个新 GPU（比如 B200），需要写什么、不能写什么？

> **答案**：新建 `tileops/perf/profiles/b200.yaml`，只写 `theoretical` 和 `calibration`（并注释 calibration 的测量来源）；**不要**手写 `effective`，否则 `_inject_effective` 里 `if "effective" not in hbm` 的条件不成立就不会覆盖你，可能与 `theoretical * calibration` 不一致。`load_profile("b200")` 自动可用，无需改任何 Python。

---

### 4.2 effective 计算

#### 4.2.1 概念说明

`effective = theoretical × calibration` 这条乘法看似简单，但它背后是一个**性能建模哲学**：用「标定过的现实天花板」当 SOL 分母，而不是用「物理理论峰值」。

为什么 calibration < 1？因为没有任何 kernel 能跑满 spec sheet 数字——HBM 有 refresh 开销、bank conflict、地址翻译；tensor core 有矩阵填充、epilogue、launch 开销。STREAM Triad 测出来 0.848，意思是「再好的 HBM kernel，在 H200 上也就到理论值的 84.8%」。用这个数当分母，算出的 SOL 效率才是一个「0%–100% 区间内、可解读」的数字——如果用 4800 GB/s 当分母，再好的 kernel 也只能到 84.8%，效率永远卡在 85% 以下，不利于判断「我的 kernel 是不是已经到顶了」。

一个反例是 H20-3e。它的 `theoretical` 是被 NVIDIA 因出口合规**人为压低**的政策数字（算力压到 H100 的约 15%），底层硅片和 H200 几乎一样。于是测出来的 kernel 性能除以这个「已经被压低的理论值」，calibration 反而**结构性地偏高**（约 0.95）。

#### 4.2.2 核心流程

`_inject_effective` 的注入逻辑分两段：

```text
对 hbm 段（单个 dict）：
    if "effective" not in hbm:
        hbm["effective"] = hbm["theoretical"] * hbm["calibration"]

对 tensor_core 段（多个 dtype 子 dict：fp16/bf16/tf32/fp8）：
    对每个 section（是 dict 且没 effective）：
        section["effective"] = section["theoretical"] * section["calibration"]
```

注意 `if "effective" not in ...` 这个守卫：它让 YAML 里即使误写了 `effective` 也不会被覆盖（保留作者意图），但规范上 YAML 不应该写这个字段。

#### 4.2.3 源码精读

**effective 注入**——hbm 是单层 dict，tensor_core 是「dtype → dict」两层，所以分两段处理：

[profile.py:60-68 — _inject_effective：对 hbm 与每个 tensor_core dtype 段算 effective](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/profile.py#L60-L68)

```python
def _inject_effective(profile):
    """Compute effective = theoretical * calibration for hbm and tensor_core."""
    hbm = profile.get("hbm")
    if hbm and "effective" not in hbm:
        hbm["effective"] = hbm["theoretical"] * hbm["calibration"]

    for section in profile.get("tensor_core", {}).values():
        if isinstance(section, dict) and "effective" not in section:
            section["effective"] = section["theoretical"] * section["calibration"]
```

这里 `isinstance(section, dict)` 是防御性的：万一以后 `tensor_core` 下混入非 dict 字段（比如一个注释串），不会崩。

**H20 的反例**——开头那段长注释解释了为什么 H20 的 calibration（~0.95）远高于 H200（~0.75）：

[h20_3e.yaml:1-16 — H20 calibration 偏高的结构性原因](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/profiles/h20_3e.yaml#L1-L16)

要点是：H20 的 `theoretical` 是「政策上限」而非「硅片天花板」，real kernel 几乎能跑满这个被压低的峰值，所以 calibration ≈ 0.95；HBM3e 颗粒和 H200 物理相同，所以 HBM 的 calibration（0.808）和 H200（0.848）接近。这段注释是 profile 文件里最重要的「为什么」——它把一个反直觉的数字解释清楚，避免后来者误以为是测量错误。

#### 4.2.4 代码实践

**实践目标**：对比 H200 与 H20-3e 的 effective 峰值，理解 calibration 的「结构性」差异。

**操作步骤**：

```python
# 示例代码
from tileops.perf.profile import load_profile

for gpu in ("h200", "h20_3e"):
    p = load_profile(gpu)
    hbm_eff = p["hbm"]["effective"]
    fp16_eff = p["tensor_core"]["fp16"]["effective"]
    print(f"{gpu:8s}  HBM eff={hbm_eff:.3e} B/s  fp16 eff={fp16_eff:.3e} FLOPS")
```

**需要观察的现象**：

- H200 的 fp16 effective（≈742 TFLOPS）远高于 H20-3e（≈141 TFLOPS）——因为 H20 算力被政策压到约 1/5。
- 但 H20 的 calibration（0.95）高于 H200（0.75）——因为它接近的是「被压低的上限」，而非硅片极限。

**预期结果**：H200 fp16 effective ≈ 989.5e12 × 0.75 ≈ 7.42e14；H20-3e fp16 effective ≈ 148e12 × 0.95 ≈ 1.406e14。（待本地验证浮点表示。）

#### 4.2.5 小练习与答案

**练习 1**：某 kernel 在 H200 上测出 actual_time，其 `bytes / actual_time = 3.5e12 B/s`。用 effective 带宽算 SOL 效率是多少？用 theoretical 呢？

> **答案**：H200 HBM effective = 4800e9 × 0.848 = 4.0704e12 B/s。用 effective：efficiency = 4.0704e12 / 3.5e12 ≈ **116%**——说明这个 kernel 已经超过标定天花板，多半是 L2 命中或测量有问题（SOL > 100% 是一个诊断信号）。用 theoretical：efficiency = 4800e9 / 3.5e12 ≈ **73%**，看起来合理但掩盖了「超过现实天花板」这个事实。这就是为什么要用 effective 而非 theoretical 当分母。

**练习 2**：为什么 `_inject_effective` 要做 `if "effective" not in section` 守卫，而不是无条件覆盖？

> **答案**：保留作者意图——如果有人在 YAML 里显式写了 `effective`（哪怕不规范），加载器不应悄悄改掉它，否则排查「为什么 effective 不等于 theoretical × calibration」会非常困难。规范是「YAML 不写 effective」，守卫只是防御性兜底。

---

### 4.3 func 公式签名

#### 4.3.1 概念说明

`formulas.py` 是 func 模式公式的总集合。每个函数对应 manifest 里某条 `roofline.func`，被 codegen 编译进对应 Op 的 `eval_roofline()`。它们都遵循**推荐签名**：

\[
\texttt{func(op)} \rightarrow (\text{flops},\ \text{bytes})
\]

`op` 是「已经 bind 好形状/dtype 的 Op 实例」——也就是说，函数被调用时，`op.m`、`op.dtype`、`op.N_total` 这些属性都已经是具体值了。函数只需读这些属性，算出 workload 的总算力和总访存量，返回两个 int。

> 注意：`formulas.py` 顶部的「老式」函数（`mha_fwd_roofline`、`gqa_fwd_roofline` 等）用的是 `op: Any | None = None, **kwargs` 的双形态签名，既接受 Op 实例也接受 kwargs 字典（经 `_shape_or_attrs` 归一）。这是历史遗留的兼容形态。**本讲推荐你关注的是新式的 `func(op)` 单参签名**（elementwise / gemm / mamba 家族），这也是 roofline.md §4.4.2 明确推荐的写法。

**什么时候必须 func 而不能用 inline？** 本讲给出三类典型场景，它们都对应 `formulas.py` 里的真实函数：

1. **多输入 dtype 字节记账**——字节流量里混了不同 dtype（bool 条件 + float 输入、fp8 输入 + fp32 scale + 高精度输出）。inline 把 `elem_bytes` 绑死在单一 dtype，表达不了。代表：`where_fwd_roofline`、`masked_fill_fwd_roofline`、`gemm_fp8_fwd_roofline`。
2. **post-broadcast 的 `N_total`**——算子广播后元素数 `product(broadcast_shapes(...))`，但 `broadcast_shapes` 不在 inline 的 vars 层命名空间（roofline.md §4.4.4）。代表：`clamp_fwd_roofline`、`masked_fill_fwd_roofline`。
3. **条件性张量存在 / 多阶段融合计数**——某些输入（dt_bias、initial_states、seq_idx）是否存在影响 cost；或一个算子融合了多个逻辑步骤需要分段求和。代表：Mamba-2 的 `_mamba2_fwd_cost`、逻辑/比较算子的 `_binary_broadcast_roofline`。

#### 4.3.2 核心流程

**func 模式如何变成 `eval_roofline`**：

```text
manifest: roofline.func = "tileops.perf.formulas.clamp_fwd_roofline"
   │
   ▼  Op 类定义时（__init_subclass__），codegen 调用
_synthesize_func_mode("ClampFwdOp", "tileops.perf.formulas.clamp_fwd_roofline")
   │
   ├─ _resolve_func_path(path)
   │     └─ rpartition(".") → ("tileops.perf.formulas", "clamp_fwd_roofline")
   │     └─ importlib.import_module + getattr → 拿到函数对象 fn（eager 解析）
   │     └─ 解析失败（模块缺失/不是 callable）→ ValueError（codegen 闸门）
   │
   └─ 返回一个闭包 eval_roofline(self): return fn(self)
   │
   ▼  装到 Op 类上
ClampFwdOp.eval_roofline = <上面那个闭包>
   │
   ▼  运行时
op.eval_roofline()  →  fn(op)  →  (flops, bytes)
```

两个关键设计：

- **eager 解析**：`_resolve_func_path` 在 codegen 期（类定义时）就把函数对象 `fn` 抓出来，闭包直接捕获它。运行时 `op.eval_roofline()` 只做 `fn(self)`，不走 import 机器，热路径零开销。
- **codegen 是权威闸门**：如果 `roofline.func` 指向一个不存在的模块或非 callable，类定义时（而非运行时）就 ValueError。一个引用坏 func 的 manifest 根本无法 land。

**func 内部如何算字节**（以多输入 dtype 为例）：先读每个输入的元素数和各自 itemsize，分别算字节再相加；不要试图找一个「统一的 elem_bytes」。

#### 4.3.3 源码精读

**(A) func 模式 codegen**——`return fn(self)` 一行就是整个方法体，`fn` 在闭包外 eager 解析好：

[_roofline_codegen.py:155-177 — _synthesize_func_mode：闭包捕获 func，emit `return fn(self)`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/_roofline_codegen.py#L155-L177)

```python
def _synthesize_func_mode(op_name, func_path):
    fn = _resolve_func_path(func_path)   # eager：类定义时解析

    def eval_roofline(self):
        return fn(self)                   # 运行时：一行调用
    eval_roofline.__name__ = "eval_roofline"
    eval_roofline.__qualname__ = f"{op_name}.eval_roofline"
    return eval_roofline
```

`_resolve_func_path` 的校验逻辑：必须是含 `.` 的字符串、模块能 import、`getattr` 出来的东西必须 callable，否则 ValueError——这就是 §4.4.2 说的「推荐签名是参考而非闸门」：codegen 不检查 `fn` 的参数个数，如果作者写了非 `func(op)` 签名，运行时 `fn(self)` 会 TypeError 直接抛给调用方。

[roofline.md:157 — func 模式契约：emit `return <func>(self)`，推荐签名 func(op)](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/roofline.md#L157)

**(B) 多输入 dtype：bool + float（where / masked_fill）**

`torch.where(cond, input, other)`：条件是 1 字节 bool，输入/输出是 float。字节流量 = 1 字节 cond + 3 份 float（input、other、out）。inline 绑死单一 `elem_bytes` 表达不了这种「1 + 3×elem_bytes」的结构：

[formulas.py:688-703 — where_fwd_roofline：1 字节 bool 条件 + 3 份 float](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L688-L703)

```python
def where_fwd_roofline(op: "Op") -> tuple[int, int]:
    n_total = int(op.N_total)
    elem_bytes = op.dtype.itemsize
    flops = n_total
    nbytes = n_total + 3 * n_total * elem_bytes   # 1字节cond + input/other/out
    return flops, nbytes
```

这里 `n_total` 是 post-broadcast 的元素数（Op 在 shape_rules 里已经算好存到 `op.N_total`），`elem_bytes` 只用于 float 部分——bool 那份直接用 `n_total × 1`。注意 flops = N_total：按 roofline.md §1.3 的约定，`where` 是「每个元素一次 predicated select」，算 1 个 flop。

`masked_fill_fwd_roofline` 是同一套思路，但只读 input + 写 out（2 份 float），加上 1 字节 mask：

[formulas.py:786-799 — masked_fill_fwd_roofline：1 字节 mask + input 读 + out 写](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L786-L799)

```python
def masked_fill_fwd_roofline(op: "Op") -> tuple[int, int]:
    n_total = int(op.N_total)
    elem_bytes = op.dtype.itemsize
    flops = n_total
    nbytes = n_total + 2 * n_total * elem_bytes   # 1字节mask + input + out
    return flops, nbytes
```

注释里特别说明「一个函数同时服务 Tensor-value 和 Scalar-value 两种变体」——因为 0-dim 的 value 张量读取相对 `N_total` 可忽略，折进 per-element 写成本即可。这就是 func 模式的灵活性：一个函数能覆盖 `variant_of` 派生出的多个 manifest 条目。

**(C) post-broadcast N_total + 融合计数（clamp）**

`torch.clamp(input, min: Tensor, max: Tensor)` 三个操作数都广播。roofline.md §1.3 说「双侧 clamp 折叠成一次 fused compare-and-select」，所以 flops = N_total（不是 2×N_total）；字节是 4 份 float（input/min/max/out）：

[formulas.py:713-723 — clamp_fwd_roofline：双侧 clamp 融合成 1 flop，4 份 float 字节](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L713-L723)

```python
def clamp_fwd_roofline(op: "Op") -> tuple[int, int]:
    n_total = int(op.N_total)
    elem_bytes = op.dtype.itemsize
    return n_total, 4 * n_total * elem_bytes      # input/min/max/out
```

为什么 clamp 必须 func 而不是 inline？两条理由叠加：(1) `N_total` 来自 `broadcast_shapes`，不在 inline vars 层命名表；(2) 即便能拿到 N_total，inline 的 `elem_bytes` 也只能绑一个 dtype——clamp 虽然三路同 dtype，但「融合成 1 flop」这个语义需要人去判断（inline 表达式 `4 * M * N` 容易，但「为什么是 1 不是 2」需要 docstring 解释）。

**(D) 比较与逻辑算子：bool 输出的字节记账**

`eq`/`gt`/`logical_and` 这类算子输出是 bool（1 字节），但输入是 float（`elem_bytes`）。`_binary_broadcast_roofline` 用一个 `bool_output` 开关统一处理整族：

[formulas.py:802-813 — _binary_broadcast_roofline：bool 输出用 1 字节，否则用 elem_bytes](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L802-L813)

```python
def _binary_broadcast_roofline(op, *, flops_per_elem, bool_output):
    a_numel = int(op.a_numel)
    b_numel = int(op.b_numel)
    n_total = int(op.N_total)
    elem_bytes = op.dtype.itemsize
    out_elem_bytes = 1 if bool_output else elem_bytes       # bool 输出 = 1 字节
    flops = flops_per_elem * n_total
    nbytes = (a_numel + b_numel) * elem_bytes + n_total * out_elem_bytes
    return flops, nbytes
```

注意字节记账的精细：读 a 和 b 用**各自真实的 numel**（广播前，a/b 可能维度不同），但输出用 post-broadcast 的 `N_total` × `out_elem_bytes`。比较算子（eq/gt/lt…）传 `bool_output=True`，输出按 1 字节算；`logical_and` / `logical_or` 传 `flops_per_elem=3`（短路求值算 3 op）且 `bool_output=True`；算术算子（add/mul）`bool_output=False`。一个 helper 函数覆盖了二十多个 binary elementwise op，这正是 func 模式相对 inline 的另一优势——可以复用共享逻辑。

**(E) FP8 GEMM：输入 dtype ≠ 输出 dtype ≠ scale dtype**

`GemmFp8Op` 三路不同 dtype：A/B 是 fp8（1 字节）、C 是高精度输出（`out_dtype`，通常 fp16/bf16）、scale 是 fp32（4 字节）。这种「输入窄、输出宽、scale 又是另一种」的结构，inline 完全表达不了：

[formulas.py:948-965 — gemm_fp8_fwd_roofline：fp8 输入 + out_dtype 输出 + fp32 scale 分开记账](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L948-L965)

```python
def gemm_fp8_fwd_roofline(op: "Op") -> tuple[int, int]:
    m, n, k = op.m, op.n, op.k
    input_bytes = op.dtype.itemsize       # fp8 = 1
    out_bytes = op.out_dtype.itemsize     # 输出 dtype，通常 2
    scale_a_shape = getattr(op, "scale_a_shape", (1, 1))
    scale_b_shape = getattr(op, "scale_b_shape", (1, 1))
    scale_elems = scale_a_shape[0]*scale_a_shape[1] + scale_b_shape[0]*scale_b_shape[1]
    flops = 2 * m * n * k
    nbytes = (m*k + n*k) * input_bytes + m*n * out_bytes + scale_elems * 4
    if getattr(op, "has_bias", False):
        nbytes += n * out_bytes
    return int(flops), int(nbytes)
```

注意两个细节：(1) scale 的 shape 用 `getattr(op, "scale_a_shape", (1,1))` 兜底 per-tensor 情形；(2) 条件性 bias 用 `if getattr(op, "has_bias", False)`——这种「配置项影响 cost」的逻辑是 func 模式的专属能力。

**(F) Mamba-2 bias 变体：条件性输入 + 多阶段求和**

Mamba-2 SSD 前向是五阶段流水（da_cumsum / cb_producer / chunk_state / state_passing / chunk_scan）。`dt_bias` 和 `initial_states` 是否存在会改变 cost，于是拆成四个 `variant_of` 公开函数，共享同一个 `_mamba2_fwd_cost(op, *, has_dt_bias, has_initial_states)`：

[formulas.py:1463-1465 — mamba2_bias_fwd_roofline：把 has_dt_bias 硬绑为 True](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L1463-L1465)

```python
def mamba2_bias_fwd_roofline(op: Any) -> tuple[int, int]:
    """Roofline for the dt_bias-consuming Mamba-2 forward variant."""
    return _mamba2_fwd_cost(op, has_dt_bias=True, has_initial_states=False)
```

`_mamba2_fwd_cost` 内部把「是否有 dt_bias」「是否有 initial_states」翻译成字节记账里的条件项（如 `+ (n_heads * 4 if has_dt_bias else 0)`），并把五阶段的 FLOP 精确求和。这种「同一套算术骨架 + 布尔开关派生多个变体」的模式在 SSD 家族里反复出现（`ssd_chunk_state_fwd_roofline` vs `..._seq_idx_...`、`ssd_state_passing_fwd_roofline` vs `..._init_states_...`、`da_cumsum_fwd_roofline` vs `da_cumsum_bias_fwd_roofline`）。

manifest 里这些变体作为 `variant_of` 条目存在，各自绑定自己的 func：

[mamba.yaml:454 — mamba2_bias 变体绑定 mamba2_bias_fwd_roofline](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/mamba.yaml#L454)

**(G) 基准如何消费 func 的输出**——`ManifestBenchmark` 只调 `op.eval_roofline()`，自己不算任何公式：

[benchmark_base.py:573-583 — ManifestBenchmark 把 flops/bytes 转发给 eval_roofline 并缓存](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L573-L583)

```python
def _get_roofline(self) -> tuple[float, float]:
    if self._roofline_cache is None:
        flops, mem_bytes = self._op.eval_roofline()   # 唯一来源
        self._roofline_cache = (float(flops), float(mem_bytes))
    return self._roofline_cache

def calculate_flops(self):
    return self._get_roofline()[0]

def calculate_memory(self):
    return self._get_roofline()[1]
```

这就是 u6-l2 强调的「禁止本地公式」：基准文件写 `(flops, bytes)` 的本地硬编码是 CI 失败。`(flops, bytes)` 必须从 `op.eval_roofline()` 来——对 func 模式 op，最终就是从 `formulas.py` 的某个函数来。

#### 4.3.4 代码实践

**实践目标**：选 `clamp_fwd_roofline`（post-broadcast + 融合计数）和 `gemm_fp8_fwd_roofline`（多输入 dtype）两个函数，说明它们如何处理字节记账，并解释为何 inline 表达不了。

**操作步骤（源码阅读型，含轻量运行）**：

1. 打开 [formulas.py:713-723](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L713-L723)（`clamp_fwd_roofline`）。回答：为什么 flops 是 `N_total` 而不是 `2 * N_total`？（提示：roofline.md §1.3 的「双侧 clamp 融合」约定。）为什么 bytes 是 `4 * N_total * elem_bytes`？这 4 份分别是什么？

2. 打开 [formulas.py:948-965](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/perf/formulas.py#L948-L965)（`gemm_fp8_fwd_roofline`）。列出三路 dtype 各自的 itemsize 和对应张量：A/B（fp8=1）、C（`out_dtype`，通常 2）、scale（fp32=4）。解释为什么 inline 模式（只有一个 `elem_bytes`）无法表达这条公式。

3. **轻量运行验证**（无 GPU，纯算术）：

```python
# 示例代码：模拟一个已 bind 的 Op，直接调用 func 验证字节记账
from tileops.perf import formulas

class _FakeClampOp:
    N_total = 4096 * 4096
    class dtype: itemsize = 2   # fp16

flops, nbytes = formulas.clamp_fwd_roofline(_FakeClampOp())
print("clamp flops:", flops, "bytes:", nbytes)
# 预期：flops = 16M；bytes = 4 * 16M * 2 = 128 MiB
```

> ⚠️ `_FakeClampOp` 是本讲为演示构造的**示例代码**，不是项目里的真实类；真实场景下 `clamp_fwd_roofline` 由 codegen 装到 `ClampFwdOp.eval_roofline` 上，接收真实的 Op 实例。

**需要观察的现象**：

- `clamp_fwd_roofline` 的 flops 与 N_total 相等（融合约定）。
- 自造的 `_FakeClampOp` 能被 func 直接调用并返回合理 `(flops, bytes)`——这验证了 func 的「推荐签名 `func(op)`」就是一个普通 Python 函数，不依赖 codegen 也能单测。

**预期结果**：clamp flops = 16777216，bytes = 134217728（= 128 MiB）。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`add_fwd_roofline` 和 `eq_fwd_roofline` 都通过 `_binary_broadcast_roofline` 实现。它们在 `flops_per_elem` 和 `bool_output` 上有何不同？为什么？

> **答案**：`add_fwd_roofline(op)` = `_binary_broadcast_roofline(op, flops_per_elem=2, bool_output=False)`（add + 一个加法进位算 2 op，输出 float）；`eq_fwd_roofline(op)` = `...（flops_per_elem=1, bool_output=True)`（一次比较算 1 op，输出 bool）。`bool_output=True` 让输出按 1 字节记账而非 `elem_bytes`——这正是 inline 单一 `elem_bytes` 表达不了的「输入 float、输出 bool」。

**练习 2**：假如你要给一个新算子写 roofline，它有两个输入 `a: float16` 和 `b: int8`，输出 `float16`。应该用 inline 还是 func？为什么？

> **答案**：必须 func。两个输入 dtype 不同（2 字节 vs 1 字节），inline 的 `elem_bytes` 只能绑一个值，无法分别给 a 和 b 计字节。func 里写 `nbytes = a_numel * 2 + b_numel * 1 + out_numel * 2` 即可。这正是 `where`（bool+float）和 `gemm_fp8`（fp8+fp32+高精度）走 func 的同一类理由。

**练习 3**：`mamba2_fwd_roofline` 和 `mamba2_bias_fwd_roofline` 为什么是两个独立函数，而不是一个带 `has_bias` 参数的函数？

> **答案**：因为 manifest 里它们是两个 `variant_of` 条目，各自有自己的 `roofline.func` 字段指向一个**确定**的可调用对象。把「是否有 dt_bias」做成 manifest 的布尔参数、再让一个函数读它，会让 roofline 依赖一个运行时参数；而 TileOPs 的做法是把变体在 manifest 层静态拆分，每个变体硬绑自己的 cost 函数（`has_dt_bias=True/False` 在函数里写死）。这让 cost 模型对每个变体都是「自包含、可单独审计」的。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，手动复现一次「从 profile + func 公式到 SOL 效率」的完整链路（不含实测耗时，用假设的 actual_time）。

**背景**：假设有一个 FP8 GEMM workload，`M=N=K=4096`，A/B 是 fp8、C 是 bf16、per-tensor scale。在一台 H200 上 actual_time = 120 µs（假设值）。算它的 SOL 效率。

**步骤**：

1. **拿硬件 effective 峰值**（模块 4.1/4.2）：

   ```python
   from tileops.perf.profile import load_profile
   p = load_profile("h200")
   hbm_eff = p["hbm"]["effective"]              # bytes/s
   fp8_eff = p["tensor_core"]["fp8"]["effective"]  # FLOPS
   ```

   预期：fp8 effective = 1979.0e12 × 0.75 ≈ 1.484e15（注：fp8 calibration 是 placeholder 0.75）。

2. **拿 workload 的 (flops, bytes)**（模块 4.3）——模拟 `gemm_fp8_fwd_roofline`：

   ```python
   # 示例代码：手算，等价于 gemm_fp8_fwd_roofline 对 m=n=k=4096 的输出
   m = n = k = 4096
   flops = 2 * m * n * k                              # = 1.374e11
   nbytes = (m*k + n*k) * 1 + m*n * 2 + 2 * 4         # fp8 A/B + bf16 C + 2×fp32 scale
   ```

   （per-tensor scale：scale_a_shape = scale_b_shape = (1,1)，共 8 字节。）

3. **算 SOL 时间与效率**（承接 u7-l1）：

   \[
   \text{compute\_time} = \frac{\text{flops}}{\text{fp8\_eff}},\quad
   \text{memory\_time} = \frac{\text{bytes}}{\text{hbm\_eff}},\quad
   \text{sol\_time} = \max(\text{compute\_time}, \text{memory\_time})
   \]

   \[
   \text{efficiency} = \frac{\text{sol\_time}}{120\,\mu s}
   \]

4. **判断 bound type**：哪一项更大就是哪一项 bound。GEMM 通常 compute-bound。

**需要观察的现象**：

- 整条链路里，`(flops, bytes)` 全部来自 manifest→func→`eval_roofline`，effective 全部来自 profile YAML，actual_time 来自基准——**三个来源严格分离，没有任何一处本地硬编码公式**。
- 如果 efficiency > 100%，说明 actual_time 假设得过于乐观（或 fp8 calibration 占位 0.75 偏低），需要复查。

> ⚠️ 本实践的具体数值「待本地验证」，重点是走通链路、理解三块数据如何汇合，而非得到一个绝对数字。

## 6. 本讲小结

- **profile 三段结构**：YAML 只存 `theoretical`（spec sheet）和 `calibration`（微基准测出的比例），`effective = theoretical × calibration` 由 `load_profile()` 在加载时注入，绝不写进 YAML。
- **effective 的意义**：用「标定过的现实天花板」当 SOL 分母，效率落在可解读的 0%–100% 区间；H20 因 theoretical 是政策压低值，calibration 结构性偏高（~0.95 vs H200 ~0.75）。
- **`_coerce_numeric_strings`** 按 key 白名单（`theoretical/calibration/effective`）强转，避开 YAML 科学计数法是字符串的问题，又不误伤 `compute_capability` 等字段。
- **func 推荐签名 `func(op) -> (flops, bytes)`**：codegen eager 解析 dotted 路径，emit `return fn(self)`；func 在闭包外捕获，运行时零 import 开销；坏路径在类定义时（codegen 闸门）就 ValueError。
- **三类必须 func 的场景**：多输入 dtype 字节记账（where/masked_fill/gemm_fp8）、post-broadcast `N_total`（clamp/masked_fill）、条件性张量与多阶段融合（Mamba bias 变体、逻辑算子）。inline 单一 `elem_bytes` 表达不了这些。
- **基准与 roofline 解耦**：M4（`ManifestBenchmark`）只调 `op.eval_roofline()` 拿 `(flops, bytes)` 并测 raw time，M5 读 profile + 这些数算效率；基准本地硬编码公式是 CI 失败。

## 7. 下一步学习建议

- **u8-l2（Roofline 代码生成）**：本讲只讲了 func 模式 codegen 的「`return fn(self)`」一行；inline 模式如何被编译成纯 Python、AST 如何校验 vars/算术两层命名空间，是 u8-l2 的主题。建议接着读 `_roofline_codegen.py` 的 inline 部分。
- **u12-l3（Mamba/SSD 家族）**：本讲把 `_mamba2_fwd_cost` 当作「条件性输入 + 多阶段求和」的 func 范例；想知道这五阶段（da_cumsum/cb/chunk_state/state_passing/chunk_scan）在 Op 层如何编排、manifest 里如何用 `variant_of` 派生 bias/init_states 变体，去 u12-l3。
- **读 `benchmarks/hardware/`**：profile 的 calibration 数字全部来自这里的微基准（`memory/hbm_bandwidth.py`、`compute/gemm_throughput.py`）。理解「0.848 是怎么测出来的」能让你彻底信服 effective 模型。
- **动手加深一个 func**：找一个还没 roofline 的 spec-only 算子，按本讲的 `func(op)` 签名为它写一个公式，体会「多输入 dtype 要分开记字节」「条件性输入用布尔开关」两条规则。
