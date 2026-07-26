# SF 宏与幂次舍入：get_sf_and_inv / load / store / transform

## 1. 本讲目标

本讲聚焦量化模块的「定标中枢」——`tile_kernels/quant/common.py` 里那一组以 `@T.macro` 形式存在的 SF（scaling factor，缩放因子）工具宏。学完后你应该能够：

- 说清 `get_sf_and_inv` 中 `clamp`、`sf`、`sf_inv` 三者的来源与关系，以及非 `round_sf` 路径的完整计算。
- 用 IEEE 754 float32 的位布局，逐步推导 `round_sf` 如何用 `(bits - 1) >> 23 + 1 - 127` 算出 \(\lceil \log_2 \rceil\) 并把 `sf` 舍入到 2 的幂。
- 解释 UE8M0 打包格式为何「就是 float32 的指数字节」，以及 `transform_sf` 如何零成本把它还原成 float32。
- 在三种 SF 物理布局（row-major、col-major、packed ue8m0）下，手工推出 `load_sf` / `store_sf` 的寻址坐标。

本讲只覆盖一个最小模块：`quant/common`。所有内容围绕这一个文件展开，并辅以 `per_token_cast_kernel.py` 中的真实调用现场。

## 2. 前置知识

本讲承接 [u4-l1](u4-l1-quant-config-basics.md)（低比特格式与 `CastInputConfig`/`CastOutputConfig` 配置体系）和 [u4-l2](u4-l2-per-token-cast-kernel.md)（`per_token_cast` 的五段骨架与两段规约）。建议你先建立以下心智模型再继续：

- **scaling factor（SF）**：低比特格式（FP8/FP4）表示范围有限（如 e4m3 最大约 448）。量化时先用一个 per-block 的 `sf = amax / max_value` 把数据「压」进范围，反量化时再乘回。`sf_inv` 是 `sf` 的倒数，专门用于反向 scale。详见 [u4-l1](u4-l1-quant-config-basics.md)。
- **absmax 定标**：`amax` 是一个 block 内绝对值最大值，是 SF 的唯一信息来源。`per_token_cast` 的规约阶段（[u4-l2](u4-l2-per-token-cast-kernel.md)）负责把它算出来交给本讲的宏。
- **IEEE 754 float32 位布局**：1 位符号 + 8 位指数 \(E\)（偏置 127）+ 23 位尾数 \(M\)。规格化数的值为

\[
  \text{value} = (-1)^s \times 2^{E-127} \times (1 + M/2^{23})
\]

本讲的位操作全部建立在这套布局上。如果你对「指数字段位于第 30…23 位」没有直觉，建议先补一下 IEEE 754 基础。
- **`@T.macro`**：TileLang 的宏机制。被 `@T.macro` 装饰的函数不是普通 Python 函数，而是在 kernel 构造期被「文本式内联展开」到调用处的代码片段——可以理解为带类型的、类型安全的宏。它和 [u2-l1](u2-l1-tilelang-anatomy.md) 讲的 `@T.prim_func`（真正的 kernel 入口）不同：宏本身不启动 kernel，只提供可复用的计算片段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/quant/common.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py) | 本讲全部内容。含 `get_sf_and_inv`、`load_sf`、`store_sf`、`transform_sf` 四个宏，以及 `CastOutputConfig.clamp_min_value`、`get_sf_shape`、`cast_epilogue` 等配套设施。 |
| [tile_kernels/quant/per_token_cast_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py) | 这些宏的「真实调用现场」。本讲用它佐证宏的输入输出语义，不深入其规约逻辑（那是 [u4-l2](u4-l2-per-token-cast-kernel.md) 的内容）。 |

---

## 4. 核心概念与源码讲解

### 4.1 get_sf_and_inv：clamp、sf 与 sf_inv 的基础计算

#### 4.1.1 概念说明

量化的最后一步是「定标」：已知一个 block 的 `amax`，要算出 `sf`（把数据压进目标格式表示范围）和 `sf_inv`（其倒数，用于反量化或就地 scale）。`get_sf_and_inv` 就是这最后一步的统一封装。

这里有一个细节必须先想清楚：**为什么需要 `clamp`？** 如果一个 block 全是 0，`amax = 0`，那么 `sf = 0`，进而 `sf_inv = max_value / 0 = ∞`，反量化时会得到 NaN。`clamp` 给 `amax` 设一个下限 `clamp_min_value`，专门防这类下溢/除零。这个下限还承担第二个职责——为 4.2 节的位操作兜底，下文会展开。

#### 4.1.2 核心流程

非 `round_sf` 路径（即 `out_config.round_sf == False`）非常直白：

```
amax                              # 规约阶段算出的块内最大绝对值
  │
  ├─ clamped_amax = max(amax, clamp_min_value)   # 防下溢/除零
  │
  ├─ sf       = clamped_amax / max_value          # 把数据压进 [−max_value, max_value]
  │
  └─ sf_inv   = max_value / clamped_amax          # 倒数，供反量化
```

`max_value` 是目标 dtype 能表示的最大值（如 e4m3 为 448），由 TileLang 内建 `T.max_value(dtype)` 给出。注意 `sf` 与 `sf_inv` 都从 **`clamped_amax`**（而非原始 `amax`）派生，二者天然互为倒数，这是保证数值一致性的关键。

#### 4.1.3 源码精读

宏的开头先 clamp，再算 `sf`（[tile_kernels/quant/common.py:196-205](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L196-L205)）：

```python
@T.macro
def get_sf_and_inv(amax: float, out_config: CastOutputConfig):
    # Clamp with min value
    clamped_amax = T.max(amax, out_config.clamp_min_value)

    max_value = T.max_value(out_config.dtype)
    sf = T.alloc_var(T.float32)
    sf = clamped_amax / max_value
    if not out_config.round_sf:
        return sf, max_value / clamped_amax
```

> 说明：`sf = T.alloc_var(T.float32)` 这一行声明了一个变量但紧接着被 `sf = clamped_amax / max_value` 覆盖，可视为历史遗留，等价于直接写 `sf = clamped_amax / max_value`。

`clamp_min_value` 是 `CastOutputConfig` 的派生属性，按 dtype 给出不同下限（[tile_kernels/quant/common.py:50-59](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L50-L59)）：

```python
    @property
    def clamp_min_value(self) -> float:
        if self.custom_clamp_min_value is not None:
            return self.custom_clamp_min_value
        elif self.dtype == T.float8_e4m3fn:
            return 1e-4
        elif self.dtype == T.float4_e2m1fn:
            return T.max_value(self.dtype) * (2**-126)
        else:
            raise ValueError(f'Unsupported dtype {self.dtype}')
```

对 e4m3 下限是 `1e-4`；对 e2m1 下限是 `6 × 2^(-126) ≈ 7.05e-38`。这两个值都**刻意选在 float32 的规格化区间内且严格大于 0**——这一点在 4.2 节会用到。

在 `per_token_cast_kernel` 里，规约得到 `sf_inv_fragment[i,j]`（即每块的 amax）后，正是调用本宏做定标（[tile_kernels/quant/per_token_cast_kernel.py:138-146](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L138-L146)）：

```python
                    amax = T.cast(amax_fragment[i, j], T.float32)
                    sf, sf_inv = get_sf_and_inv(amax, out_config)
                    ...
                    store_sf(out_sf, sf, m_idx, k_idx, out_config)
                    sf_inv_fragment[i, j] = sf_inv
```

返回的 `sf` 被写出到 `out_sf`，`sf_inv` 被留作后续 scale 输入之用。

#### 4.1.4 代码实践

这是一个**纯 Python 源码阅读型实践**，无需 GPU。

1. **目标**：手算 `get_sf_and_inv` 非 round 路径，验证 `sf × sf_inv == 1`。
2. **步骤**：取 `fmt='e4m3'`（`max_value=448`，`clamp_min_value=1e-4`），对 `amax ∈ {0.0, 1e-5, 3.7, 448.0, 1000.0}` 分别手算 `clamped_amax`、`sf`、`sf_inv`。
3. **观察现象**：注意 `amax=0.0` 与 `amax=1e-5` 两种情况都会被 clamp 拉到 `1e-4`。
4. **预期结果**：每个 `amax` 都有 `sf × sf_inv == 1.0`；被 clamp 的两项 `sf` 相同。
5. **待本地验证**：以上可手算，建议在 Python 里用 `max(amax,1e-4)/448` 与 `448/max(amax,1e-4)` 复核。

#### 4.1.5 小练习与答案

**练习 1**：若去掉 `clamp`，当某行输入全为 0 时会发生什么？

**答案**：`amax=0 → sf=0 → sf_inv=∞`，后续 `out = x * sf_inv` 会得到 0×∞=NaN，反量化也会失败。`clamp` 把下限抬到 `1e-4`，使 `sf_inv` 始终有限。

**练习 2**：`sf` 与 `sf_inv` 为何都要从 `clamped_amax` 而非 `amax` 派生？

**答案**：若一个用 `clamped_amax`、另一个用 `amax`，被 clamp 时二者不再严格互为倒数，反量化会引入系统性误差。统一从 `clamped_amax` 派生保证 `sf × sf_inv == max_value² / clamped_amax²` 的互倒关系（在该非 round 路径下恰好 `sf × sf_inv = (clamped/max_value)(max_value/clamped)=1`）。

---

### 4.2 round_sf：幂次舍入的位操作原理

#### 4.2.1 概念说明

当 `out_config.round_sf == True`，`sf` 会被强制舍入到**最近的 2 的幂（向上取整）**。为什么要这么做？

1. **配合 UE8M0 格式**：4.3 节会讲，UE8M0 只能表示 2 的幂。要输出 UE8M0 的 SF，`sf` 本身必须是 2 的幂。
2. **`sf_inv` 变成精确的 2 的幂**：在反量化时，乘一个 \(2^k\) 等价于「指数加 \(k\)」，硬件上可廉价完成，且 `sf_inv` 自身无精度损失。
3. **舍入方向选「向上」**：`sf` 变大意味着 `sf_inv` 变小，输入被压得更狠，量化后的幅值更小——这是**不会溢出**的安全方向（绝不会因为舍入而超出 `max_value` 截断）。

#### 4.2.2 核心流程

关键是求

\[
\text{exp\_sf} = \lceil \log_2(\text{sf}) \rceil
\]

然后 `sf = 2^exp_sf`，`sf_inv = 2^{-exp_sf}`。源码用一行位操作完成 `ceil(log2)`：

```
bits    = reinterpret(sf, uint32)            # 取 float32 的 32 位模式
exp_sf  = ((bits - 1) >> 23) + 1 - 127       # 等价于 ceil(log2(sf))
sf      = reinterpret((127 + exp_sf) << 23, float32)   # 2^exp_sf
sf_inv  = reinterpret((127 - exp_sf) << 23, float32)   # 2^(-exp_sf)
```

**推导为什么 `(bits - 1) >> 23 + 1 - 127` 恰好是 \(\lceil\log_2\rceil\)。** 设 `sf` 为正规格化 float32，符号位为 0，故 `bits = (E << 23) | M`，其中 \(E\) 是 8 位指数字段（已含偏置 127），\(M \in [0, 2^{23}-1]\) 是尾数。真正的指数是 \(e = E - 127\)，于是 \(\text{sf} \in [2^e, 2^{e+1})\)。

- **当 \(M = 0\)**（sf 恰为 \(2^e\)）：`bits-1` 会从指数借位，得 `(E-1) << 23 | (2^23 - 1)`，右移 23 位后等于 \(E-1\)。再加 1 减 127：\((E-1)+1-127 = E-127 = e\)。此时 \(\lceil\log_2\rceil = \log_2 = e\)。✓
- **当 \(M \neq 0\)**（sf 严格落在 \((2^e, 2^{e+1})\)）：`bits-1` 只把尾数减 1（\(M \geq 1\) 所以不借位），指数字段仍是 \(E\)，右移 23 位得 \(E\)。再加 1 减 127：\(E+1-127 = e+1\)。此时 \(\lceil\log_2\rceil = e+1\)。✓

两种情形合并，公式正好给出 \(\lceil\log_2(\text{sf})\rceil\)。重建时把目标指数 \(e' = \text{exp\_sf}\) 写回指数字段（\(127 + e'\)）、尾数清零，就得到 \(2^{e'}\)。

> **前置条件的兜底**：上述推导要求 `sf` 是「正、规格化、非零」的 float32。这恰好由 4.1 节的 `clamp_min_value` 保证——源码注释写得很明确（[tile_kernels/quant/common.py:209](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L209)）：`amax >= 1e-4` 保证符号位为 0 且 `bits != 0`（无 denorm/零）。对 e2m1，下限 `6×2^(-126)` 同样落在规格化区间（最小规格化正数是 \(2^{-126}\)）。这就是为什么 clamp 不只是防除零，更是位操作正确性的前提。

#### 4.2.3 源码精读

`round_sf` 路径的全部代码（[tile_kernels/quant/common.py:207-216](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L207-L216)）：

```python
    # Round into 2's power
    bits = T.reinterpret(sf, T.uint32)
    # amax >= 1e-4 ensures sign bit = 0 and bits != 0 (no denorm/zero).
    # `(bits - 1) >> 23 + 1` gives ceil(log2).
    exp_sf = ((bits - 1) >> 23) + 1 - 127
    sf_inv = T.reinterpret((127 - exp_sf) << 23, T.float32)
    if out_config.use_packed_ue8m0:
        return T.uint8(exp_sf + 127), sf_inv
    else:
        return T.reinterpret((127 + exp_sf) << 23, T.float32), sf_inv
```

`sf_inv`（[第 212 行](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L212)）两种返回分支共用，因为无论 `sf` 存成 float32 还是 UE8M0，它的值都是 \(2^{\text{exp\_sf}}\)，倒数都是 \(2^{-\text{exp\_sf}}\)。`T.reinterpret(x, dtype)` 是 TileLang 的位重解释内建，等价于 C 的 `union` / Python 的 `struct.unpack`。

> 注意 `<<` 与 `+` 的优先级：`(127 - exp_sf) << 23` 的括号是必要的，源码确实加了括号；而 `(bits - 1) >> 23) + 1` 这一步源码写的是 `((bits - 1) >> 23) + 1 - 127`，移位先于加减，符合预期。

#### 4.2.4 代码实践

**目标**：用纯 Python 复现 `round_sf` 逻辑，验证返回的 `sf` 恒为 2 的幂、`sf × sf_inv == 1`。

```python
# 示例代码（非项目原有，用于复现 common.py 的 round_sf 逻辑）
import struct, math

def f2u(x):  # float32 位模式 -> uint32
    return struct.unpack('<I', struct.pack('<f', x))[0]

def u2f(u):  # uint32 -> float32
    return struct.unpack('<f', struct.pack('<I', u))[0]

MAX_VALUE = 448.0      # e4m3
CLAMP_MIN = 1e-4

def get_sf_and_inv_round(amax):
    clamped = max(amax, CLAMP_MIN)
    sf = clamped / MAX_VALUE
    bits = f2u(sf)
    exp_sf = ((bits - 1) >> 23) + 1 - 127
    sf_inv = u2f((127 - exp_sf) << 23)
    sf_pow2 = u2f((127 + exp_sf) << 23)
    return sf, sf_pow2, sf_inv, exp_sf

for amax in [0.0, 1e-5, 3.7, 100.0, 448.0, 1000.0]:
    sf_raw, sf_pow2, sf_inv, e = get_sf_and_inv_round(amax)
    lg = math.log2(sf_pow2)
    print(f"amax={amax:>8}: exp_sf={e:>3}, sf=2^{e}={sf_pow2}, "
          f"是2的幂={lg == int(lg)}, sf*sf_inv={sf_pow2*sf_inv}")
```

**操作步骤**：把脚本存为本地 `.py` 文件运行（无需 GPU，纯 CPU 即可）。

**预期结果**：每一行 `是2的幂=True`，`sf*sf_inv=1.0`。例如 `amax=3.7` 时 `sf = 3.7/448 ≈ 0.00826`，落在 \((2^{-7}, 2^{-6})\)，故 `exp_sf = -6`，`sf_pow2 = 2^{-6} = 0.015625`（向上舍入，确实 ≥ 原始 sf）。

**待本地验证**：如果你跑出 `amax=3.7 → sf=0.015625`，说明位操作复现成功。

#### 4.2.5 小练习与答案

**练习 1**：为什么舍入方向是「向上」（ceil）而不是「四舍五入」或「向下」？

**答案**：向上舍入让 `sf` 偏大、`sf_inv` 偏小，量化时把数据压得更狠，保证幅值不会因舍入而越过 `max_value` 被截断——这是避免溢出的安全方向。

**练习 2**：把 `sf = 3.7/448` 代入公式，手算 `bits-1 >> 23 + 1 - 127`。

**答案**：`sf ≈ 0.00826 = 2^{-7} × 1.86`，故 \(E = 120\)（\(e=-7\)），\(M \neq 0\)。`bits-1` 不借位，`(bits-1)>>23 = 120`，`+1-127 = -6`。即 \(\lceil\log_2(0.00826)\rceil = -6\)。✓

---

### 4.3 UE8M0 打包格式与 transform_sf

#### 4.3.1 概念说明

UE8M0 = **U**nsigned、**8** 位指数、**0** 位尾数。一个 UE8M0 字节只能表示 \(2^{k}\)（\(k\) 为某整数），刚好就是 4.2 节舍入后的 `sf`。它的精妙之处在于存储编码：**UE8M0 字节直接等于 float32 表示中的「指数字段」**，因此与 float32 互转几乎是零成本的位搬运。

- 写入时（`get_sf_and_inv` 的 ue8m0 分支）：存 `exp_sf + 127`，即把目标指数 \(e'\) 加偏置 127 写成一个字节。
- 读出时（`transform_sf`）：把这个字节放回 float32 的指数字段（左移 23 位），尾数和符号都为 0，重新解释成 float32 就还原出 \(2^{e'}\)。

之所以用 UE8M0 而非 float32 存 SF，是为了省存储：1 字节 vs 4 字节，SF 张量体积直接缩到 1/4，对带宽敏感的量化场景收益显著。代价是 SF 必须是 2 的幂（所以才有 4.2 的 `round_sf`）。

#### 4.3.2 核心流程

UE8M0 的「存储 → 还原」闭环：

```
get_sf_and_inv (ue8m0 分支)
    存 sf_byte = exp_sf + 127            # 一个 uint8

transform_sf
    还原 = reinterpret( uint32(sf_byte) << 23, float32 )
         = 2^(sf_byte - 127) = 2^exp_sf   # 与 float32 分支的 sf 值相同
```

「打包」则是把 4 个 UE8M0 字节塞进一个 `uint32`/`int32`，由 `cast_epilogue` 用 `.view(dtype=torch.int32)` 完成（见 4.4 节的布局讨论）。注意 `get_sf_and_inv` 的 ue8m0 分支返回的 `sf_inv` 仍是 float32（[第 212 行](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L212)），因为 `sf_inv` 只在 kernel 内部用于 scale，不写出，没必要也存成 UE8M0。

#### 4.3.3 源码精读

`get_sf_and_inv` 的 ue8m0 返回分支（[tile_kernels/quant/common.py:213-216](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L213-L216)）：

```python
    if out_config.use_packed_ue8m0:
        return T.uint8(exp_sf + 127), sf_inv
    else:
        return T.reinterpret((127 + exp_sf) << 23, T.float32), sf_inv
```

注意两个分支返回的 `sf` **数值相等**：ue8m0 分支返回的字节 `exp_sf + 127`，与 float32 分支 `(127+exp_sf)<<23` 的指数字段完全一致——只是前者砍掉了符号位和尾数位。

`transform_sf` 把 UE8M0 字节还原回 float32（[tile_kernels/quant/common.py:229-234](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L229-L234)）：

```python
@T.macro
def transform_sf(sf: Union[T.float32, T.uint8], config: BaseCastConfig) -> T.float32:
    if config.use_packed_ue8m0:
        return T.reinterpret(T.uint32(sf) << 23, T.float32)
    else:
        return sf
```

`T.uint32(sf) << 23` 把字节放进指数字段（bits 30…23），符号位 0、尾数 0，reinterpret 后即 \(2^{\text{sf}-127}\)。若不是 ue8m0，SF 本就是 float32，直接透传。

「打包成 int32」发生在 kernel 启动后的后处理 `cast_epilogue`（[tile_kernels/quant/common.py:186-191](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L186-L191)）：

```python
    if config.use_packed_ue8m0:
        if num_tokens == 0:
            out_sf = torch.empty((out_sf.shape[0], out_sf.shape[1] // 4), dtype=torch.int32, device=out_sf.device)
        else:
            out_sf = out_sf.view(dtype=torch.int32)
    out_sf = out_sf.T if config.use_tma_aligned_col_major_sf else out_sf
```

kernel 以 uint8 视图写入 4 的倍数个字节，这里 `.view(dtype=torch.int32)` 把每 4 个字节重新解释成一个 int32，完成「4 UE8M0 → 1 int32」的无拷贝打包。

#### 4.3.4 代码实践

**目标**：验证 UE8M0 字节与 float32 的互转等价于 4.2 节的 float32 舍入值。

```python
# 示例代码
import struct
def u2f(u): return struct.unpack('<f', struct.pack('<I', u))[0]

exp_sf = -6                       # 取 4.2 节 amax=3.7 的例子
sf_byte = exp_sf + 127            # = 121，UE8M0 存储字节
sf_restored = u2f(sf_byte << 23)  # transform_sf 的纯 Python 复现
print(sf_byte, sf_restored)       # 期望 121, 0.015625
```

**预期结果**：`sf_byte=121`，`sf_restored=0.015625`，与 4.2 节 float32 分支的 `sf_pow2` 完全相等。

**待本地验证**：运行后确认两个分支数值一致。

#### 4.3.5 小练习与答案

**练习 1**：UE8M0 的 1 字节为什么能无损表达 `round_sf` 后的 `sf`？

**答案**：`round_sf` 已把 `sf` 限制为 \(2^k\)，只需存指数 \(k\)。加 127 偏置后落在 uint8 范围内（\(k \in [-127, 128]\)），一个字节足矣。

**练习 2**：为何 `sf_inv` 不也存成 UE8M0？

**答案**：`sf_inv` 只在 kernel 内部参与 scale 计算，用完即弃、不写出到显存，存成 UE8M0 没有省存储的收益，反而要额外 `transform_sf` 还原，所以保持 float32。

---

### 4.4 load_sf / store_sf：多布局分发

#### 4.4.1 概念说明

同一份逻辑 SF（用 `(m_idx, k_idx)` 标识，`m_idx` 是 token 维块号、`k_idx` 是 hidden 维块号）在显存里有**三种物理布局**，由 `BaseCastConfig` 的两个布尔位决定：

| 布局 | 触发条件 | 物理形状（由 `get_sf_shape` 给出） |
| --- | --- | --- |
| row-major | 默认 | `(num_block_m, num_block_k)` |
| col-major (TMA aligned) | `use_tma_aligned_col_major_sf=True` | `(num_block_k, num_block_m)` |
| packed ue8m0 | `use_packed_ue8m0=True`（隐含 col-major） | `(ceil(num_block_k/4), num_block_m*4)`，dtype=uint8 |

`load_sf` / `store_sf` 就是这三套寻址的统一入口：**给定逻辑坐标 `(m_idx, k_idx)` 和 config，返回/写入正确的物理地址**。这样 kernel 主体只需写一遍，布局差异全部被宏吸收。

#### 4.4.2 核心流程

三个分支的寻址（以 `load_sf` 为例，`store_sf` 完全对称）：

```
row-major : tensor[ m_idx        ,  k_idx           ]
col-major : tensor[ k_idx        ,  m_idx           ]
packed    : tensor[ k_idx // 4   ,  m_idx * 4 + k_idx % 4 ]
```

packed 分支的含义：把 hidden 方向每 4 个连续 SF 块「打包」——`k_idx // 4` 选第几个打包组（行），`k_idx % 4` 选组内第几个字节；而 `m_idx * 4` 给每个 token 块预留 4 列连续字节。结果是把同 token 的 4 个 SF 字节放在一起，正好凑成一个 int32（见 4.3 的 `.view(int32)`）。

物理形状由 `get_sf_shape` 决定（[tile_kernels/quant/common.py:130-138](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L130-L138)）：

```python
def get_sf_shape(shape, config: BaseCastConfig):
    num_block_m = ceil_div(shape[0], config.sf_block[0])
    num_block_k = ceil_div(shape[1], config.sf_block[1])
    if config.use_packed_ue8m0:
        num_block_m = num_block_m * 4
        num_block_k = ceil_div(num_block_k, 4)
    return (num_block_k, num_block_m) if config.use_tma_aligned_col_major_sf else (num_block_m, num_block_k)
```

#### 4.4.3 源码精读

`load_sf`（[tile_kernels/quant/common.py:219-226](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L219-L226)）：

```python
@T.macro
def load_sf(tensor: T.Tensor, m_idx: int, k_idx: int, config: BaseCastConfig):
    if config.use_packed_ue8m0:
        return tensor[k_idx // 4, m_idx * 4 + k_idx % 4]
    elif config.use_tma_aligned_col_major_sf:
        return tensor[k_idx, m_idx]
    else:
        return tensor[m_idx, k_idx]
```

`store_sf`（[tile_kernels/quant/common.py:237-244](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L237-L244)）与 `load_sf` 寻址完全一致，只是把 `return tensor[...]` 换成 `tensor[...] = sf`。

真实调用现场——`per_token_cast_kernel` 读输入 SF 时，先 `load_sf` 再 `transform_sf` 还原成 float32（[tile_kernels/quant/per_token_cast_kernel.py:91-94](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L91-L94)）：

```python
                for i, j in T.Parallel(num_sf_rows_per_block, num_sf_cols_per_block):
                    m_idx = pid_token * block_m // in_config.sf_block[0] + i
                    k_idx = pid_hidden * block_k // in_config.sf_block[1] + j
                    x_sf_fragment[i, j] = transform_sf(load_sf(x_sf, m_idx, k_idx, in_config), in_config)
```

写出输出 SF 时则用 `store_sf`（[tile_kernels/quant/per_token_cast_kernel.py:116-118](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L116-L118)）。注意读路径用 `in_config`、写路径用 `out_config`——输入和输出的 SF 布局可以不同，宏按各自的 config 自动寻址。

#### 4.4.4 代码实践

**目标**：给定一组形状与 config，手算三种布局下的物理坐标，确认与 `get_sf_shape` / `load_sf` 一致。

设 `shape=(num_tokens=8, hidden=128)`，`sf_block=(1, 32)`，则 `num_block_m=8`、`num_block_k=4`。对逻辑坐标 `(m_idx=2, k_idx=3)`：

1. **步骤**：分别按 row-major、col-major、packed ue8m0 三种 config，写出 `get_sf_shape` 的返回形状与 `load_sf` 的物理下标。
2. **观察现象**：
   - row-major：形状 `(8, 4)`，下标 `tensor[2, 3]`。
   - col-major：形状 `(4, 8)`，下标 `tensor[3, 2]`。
   - packed：形状 `(ceil(4/4)=1, 8*4=32)`，下标 `tensor[3//4, 2*4+3%4] = tensor[0, 11]`。
3. **预期结果**：三种布局的物理坐标如上，均落在各自形状范围内。
4. **待本地验证**：可在 Python 里构造三种 `torch.empty` 张量，按上述坐标写入再读回，确认无越界。

#### 4.4.5 小练习与答案

**练习 1**：为什么 packed 分支要把 hidden 方向的 4 个 SF 块打包，而不是 token 方向？

**答案**：因为同一个 token 的多个 hidden 子块 SF 经常被一起消费（kernel 沿 hidden 维分块处理同一行），把它们放进同一个 int32 / 连续 4 字节，能让 `transform_sf` 后的访问局部性更好，也方便 `.view(int32)` 一次性吐出 4 个字节。源码注释也写明「4 UE8M0 are expanded into the inner dim (token)」——指 `num_block_m` 维被 ×4 展开，物理上 4 个 hidden 子块的 SF 沿 token 内维并排存放。

**练习 2**：`load_sf` 用 `in_config` 而 `store_sf` 用 `out_config`，这意味着什么？

**答案**：输入 SF 和输出 SF 的布局可以独立配置。例如输入是 row-major float32、输出是 packed ue8m0，kernel 主体代码完全不用改，布局差异由宏按各自 config 自动处理——这正是把这组操作封装成 `@T.macro` 的核心收益。

---

## 5. 综合实践

把本讲四个宏串起来，用**纯 Python**（无需 GPU）复现 `get_sf_and_inv` 的完整逻辑（含 `round_sf` 与 UE8M0 两个分支），并构造一组 `amax` 做端到端验证：

```python
# 示例代码：复现 common.py 的 get_sf_and_inv + transform_sf 闭环
import struct, math

def f2u(x): return struct.unpack('<I', struct.pack('<f', x))[0]
def u2f(u): return struct.unpack('<f', struct.pack('<I', u))[0]

MAX_VALUE = 448.0
CLAMP_MIN = 1e-4

def get_sf_and_inv(amax, round_sf, use_packed_ue8m0):
    clamped = max(amax, CLAMP_MIN)
    sf = clamped / MAX_VALUE
    if not round_sf:
        return sf, MAX_VALUE / clamped
    bits = f2u(sf)
    exp_sf = ((bits - 1) >> 23) + 1 - 127
    sf_inv = u2f((127 - exp_sf) << 23)
    if use_packed_ue8m0:
        return exp_sf + 127, sf_inv           # UE8M0 字节
    return u2f((127 + exp_sf) << 23), sf_inv  # float32

def transform_sf(sf, use_packed_ue8m0):
    return u2f(sf << 23) if use_packed_ue8m0 else sf

for amax in [0.0, 1e-5, 3.7, 100.0, 1000.0]:
    # 分支 A：round_sf + float32
    sfA, invA = get_sf_and_inv(amax, True, False)
    # 分支 B：round_sf + UE8M0（经 transform_sf 还原）
    byteB, invB = get_sf_and_inv(amax, True, True)
    sfB = transform_sf(byteB, True)
    lg = math.log2(sfA)
    assert lg == int(lg), "sf 不是 2 的幂"
    assert sfA == sfB, "UE8M0 还原后应与 float32 分支相等"
    assert abs(sfA * invA - 1.0) < 1e-6, "sf*sf_inv 应为 1"
    print(f"amax={amax:>8}: sf=2^{int(lg):>3}={sfA:<12} ue8m0_byte={byteB:>3} OK")
```

**操作步骤**：本地运行该脚本（纯 CPU）。

**预期结果**：所有断言通过；每个 `sf` 恒为 2 的幂；UE8M0 分支经 `transform_sf` 还原后与 float32 分支数值相等；`sf × sf_inv == 1`。

**思考延伸**（源码阅读型）：在 [per_token_cast_kernel.py:113-119](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L113-L119) 处，`get_sf_and_inv` 返回的 `sf` 被 `store_sf` 写出、`sf_inv` 被留作 scale。请回答：当 `use_packed_ue8m0=True` 时，写出的是字节还是 float32？`sf_inv` 又是哪种？为什么？

> 参考答案：写出的是 UE8M0 字节（`exp_sf+127`），`sf_inv` 仍是 float32；因为 `sf` 要长期存显存、省存储优先，而 `sf_inv` 只在 kernel 内部用完即弃、无需省存储。

## 6. 本讲小结

- `get_sf_and_inv` 是量化定标的统一入口：`clamp` 防 `amax=0` 的下溢与除零，非 round 路径直接返回 `sf=clamped/max_value` 与其倒数 `sf_inv`。
- `round_sf` 用 `(bits-1)>>23 + 1 - 127` 一行位操作求出 \(\lceil\log_2(\text{sf})\rceil\)，把 `sf` 向上舍入到 2 的幂——安全（不溢出）且为 UE8M0 铺路。
- 该位操作依赖 `sf` 为「正、规格化、非零」的 float32，这一前提由 `clamp_min_value`（e4m3 为 `1e-4`、e2m1 为 `6×2^{-126}`）保证——clamp 同时承担防除零与位操作兜底两重职责。
- UE8M0 把 SF 压成 1 字节，存储值 `exp_sf+127` 恰好就是 float32 的指数字段；`transform_sf` 用 `uint32(byte)<<23` 零成本还原成 float32。
- `load_sf` / `store_sf` 用三分支吸收 row-major / col-major / packed ue8m0 三种物理布局，使 kernel 主体与布局解耦，且读写可分别用 `in_config`/`out_config` 独立配置。

## 7. 下一步学习建议

- 本讲的宏都是为 cast kernel 服务的。建议进入 [u4-l4](u4-l4-block-lossless-e5m6-castback.md)，看 `per_block_cast`、`per_block_cast_lossless`、`per_token_cast_to_e5m6` 与 `cast_back` 如何复用这套 `get_sf_and_inv` / `load_sf` / `store_sf`，以及 `cast_back` 反量化的精度损失来源。
- 若想看这些宏在「带输入 SF 的两段规约」里如何被调用，可回看 [u4-l2](u4-l2-per-token-cast-kernel.md) 的 `with_sf` 分支与本讲引用的 [per_token_cast_kernel.py:91-94](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L91-L94)。
- 后续 [u10-l2](u10-l2-tma-vectorize-layout.md) 会从硬件视角重新讨论 TMA 对齐、`get_best_vectorize_size` 与布局选择，届时可把本讲的三种 SF 布局与 TMA 的 16 字节对齐要求（`alloc_scaling_factors` 里的 `align(..., 16)`）对应起来读。
