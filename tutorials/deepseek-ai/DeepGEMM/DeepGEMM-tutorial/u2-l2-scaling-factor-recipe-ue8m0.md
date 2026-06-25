# 缩放因子 recipe 与 UE8M0 打包

## 1. 本讲目标

本讲聚焦 DeepGEMM 里一个看起来很小、却横跨 Python / C++ / 硬件三层的关键概念：**缩放因子（Scaling Factor，简称 SF）**。FP8 / FP4 的数值范围很窄，必须配合「逐块缩放」才能表示真实张量；而这些缩放因子在喂给 GPU tensor core 之前，还要被重排成一种特殊的 TMA 友好布局，并且在不同架构上用完全不同的数据格式存放。

学完本讲你应该能够：

1. 解释「逐块缩放」为什么是 FP8/FP4 计算的前提，并用 `recipe=(gran_mn, gran_k)` 描述缩放粒度。
2. 说出 SM90 与 SM100 上 SF 格式的核心差异：SM90 用 **FP32**，SM100 用**打包 UE8M0（4 个打包进一个 `torch.int`）**。
3. 看懂 `transform_sf_into_required_layout` 如何根据 `(dtype, gran_mn, gran_k, arch)` 把「用户提供的 SF」变换成「kernel 需要的 TMA 对齐布局」，并能手动推导给定 `[M, K]` 下 SF 的形状与 dtype。

本讲承接 u2-l1 建立的 `D = C + A @ B`、NT 布局与 `arch_major`（9 vs 10）概念；它的产出会直接服务于 u2-l3（C++ 绑定与派发）与 u4-l2（TMA 描述符）。

## 2. 前置知识

在进入源码前，先用三条直觉建立心智模型。

**直觉一：FP8 太「窄」，必须逐块配一个比例尺。** FP8 的 `e4m3` 格式最大只能表示 `448`。如果一个 `[M, K]` 的真实张量里既有 `0.001` 也有 `1000`，直接转成 FP8 会把大数截断、小数淹没。解决办法是把张量切成小块，每块算一个「比例尺」\(sf\)，让该块所有元素除以 \(sf\) 后都落进 `[-448, 448]`：

\[
sf = \frac{\mathrm{amax}(\text{block})}{448}, \qquad q = x / sf
\]

这样存储的是「压缩后的整数 \(q\)」加「比例尺 \(sf\)」，二者配对才能还原原值。这就是 **per-block scaling**（逐块缩放）。

**直觉二：「切块多大」就是 recipe。** 一个块在 M/N 方向跨 `gran_mn` 个元素、在 K 方向跨 `gran_k` 个元素。`gran_mn` 和 `gran_k` 越大，SF 越少（省显存、省带宽），但精度越粗。DeepGEMM 把这套粒度参数打包成 **recipe**，后续所有逻辑都以它为输入。

**直觉三：比例尺本身也要被「排好版」。** 用户用 PyTorch 工具算出的 SF 是一种「自然连续」布局（例如 `[M, K/128]` 的行主序 float）。但 tensor core 通过 TMA 异步搬运数据时，要求 SF 是 **MN-major（列主序）且按 16 字节对齐** 的，SM100 上甚至要求 SF 被压缩成 UE8M0 这种硬件原生格式。所以 API 内部必须做一次「版式变换」——这正是 `transform_sf_into_required_layout` 的工作。

> 名词速查：
> - **SF（Scaling Factor）**：缩放因子，即上面的比例尺。
> - **SFA / SFB**：分别指矩阵 A、矩阵 B 的 SF。
> - **MN / K**：在 GEMM `D = C + A @ B` 里，A 的「行数 M」与 B 的「行数 N」统称 MN 轴，A/B 共享的「收缩轴」是 K 轴。recipe 里的 `gran_mn` 描述 MN 轴粒度，`gran_k` 描述 K 轴粒度。
> - **UE8M0**：Unsigned Exponent 8-bit, Mantissa 0-bit，即「8 位无符号指数、0 位尾数」，等价于只保留一个正浮点的指数部分，只能表示 2 的整数次幂。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它看什么 |
|------|------|----------------|
| [deep_gemm/utils/math.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/math.py) | 纯 Python 量化工具 | `per_token_cast_to_fp8` 如何生成「用户格式」的 SF；`ceil_to_ue8m0` / `pack_ue8m0_to_int` 如何造 UE8M0 |
| [csrc/utils/layout.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp) | C++ 侧 recipe 与 SF 校验 | `get_default_recipe`（默认粒度）、`check_sf_layout`（形状/格式断言） |
| [csrc/apis/layout.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp) | SF 变换 API 层 | `transform_sf_into_required_layout` 的四条分支 |
| [csrc/jit_kernels/impls/smxx_layout.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_layout.hpp) | 真正的变换/打包 CUDA kernel | `get_mn_major_tma_aligned_tensor`（SM90 转置）、`get_mn_major_tma_aligned_packed_ue8m0_tensor`（SM100 打包） |
| [deep_gemm/utils/layout.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/layout.py) | Python 再导出 | 把 C++ 侧变换/对齐函数挂到 `deep_gemm.utils` 命名空间 |
| [tests/test_layout.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_layout.py) | SF 布局正确性测试 | 变换结果与纯 PyTorch 参考实现逐位比对 |

## 4. 核心概念与源码讲解

### 4.1 逐块缩放与 recipe

#### 4.1.1 概念说明

「recipe」就是描述「SF 怎么切」的一组整数。在 DeepGEMM 里它有两种写法（见 `transform_sf_into_required_layout` 的参数）：

- **三元素 `(gran_mn_a, gran_mn_b, gran_k)`**：A、B 可以有不同的 MN 粒度，但共享同一个 K 粒度。适合「A 按 token 缩放、B 按大方块缩放」这种不对称场景。
- **两元素 `(gran_mn, gran_k)`**：成对出现，即分别给 A、B 各一个 `(gran_mn, gran_k)`。

`gran_k` 通常取 `128`（或 SM100 上的 `32`）。为什么是 128？因为 tensor core 一次 MMA 在 K 方向吃 128 个 FP8 元素（详见 u6），把缩放粒度对齐到 MMA 粒度，可以让「一个 SF 管住恰好一次乘加需要的 K 段」，省去频繁查表。

#### 4.1.2 核心流程

一次「用户侧生成 SF」的标准流程（以最常见的 per-token 量化为例）：

```
x : [M, K] (BF16)
 │  按 gran_k 把 K 切成 K/gran_k 段
 ▼
对每段求 amax → [M, K/gran_k]
 │  sf = amax / 448
 ▼
FP32 形式的 SF : [M, ceil(K/gran_k)]        ← 这就是「用户格式」
```

如果是 UE8M0 路径，还会再走 `ceil_to_ue8m0`（向上取整到 2 的幂）。这条流程的代码就是 `per_token_cast_to_fp8`：

```python
x_view = x_padded.view(m, padded_n // gran_k, gran_k)
x_amax = x_view.abs().float().amax(dim=2).view(m, padded_n // gran_k).clamp(1e-4)
sf = x_amax / 448.0
sf = ceil_to_ue8m0(sf) if use_ue8m0 else sf
x_fp8 = (x_view * (1.0 / sf.unsqueeze(2))).to(torch.float8_e4m3fn)
```

参见 [deep_gemm/utils/math.py:26-38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/math.py#L26-L38)，其中 `clamp(1e-4)` 是为了避免 amax 为 0 时除零；`/ 448.0` 正是上面直觉一里的比例尺分母。

**默认 recipe 是谁定的？** 当用户不显式传 `recipe` 时，C++ 侧用 `get_default_recipe(sfa_dtype, sfb_dtype)` 给出默认值：

```cpp
if (arch_major == 9) {
    DG_HOST_ASSERT(sfa_dtype == torch::kFloat and sfb_dtype == torch::kFloat);
    return {1, 128, 128};                       // SM90: A 逐 token, B 按 128 块
} else if (arch_major == 10) {
    return sfb_dtype == torch::kFloat ?
        std::make_tuple(1, 128, 128):           // SM100 旧格式(FP32)
        std::make_tuple(1,   1, 128);           // SM100 1D1D 内核
}
```

参见 [csrc/utils/layout.hpp:64-77](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp#L64-L77)。这里能看出 recipe 与架构强绑定：

| 架构 | 默认 recipe `(gran_mn_a, gran_mn_b, gran_k)` | 含义 |
|------|----------------------------------------------|------|
| SM90 | `(1, 128, 128)` | A 逐行（token）缩放、B 按 128×128 块缩放 |
| SM100（FP32 旧） | `(1, 128, 128)` | 与 SM90 同粒度，但格式不同 |
| SM100（1D1D） | `(1, 1, 128)` | A、B 都逐行（1D）缩放 |

> 旁注：`(1, 128, 128)` 对应 SM90 的「1D2D」内核（SFA 是 1D、SFB 是 2D 块），`(1, 1, 128)` 对应 SM100 的「1D1D」内核。内核类型与 recipe 的对应关系会在 u5、u6 详讲，这里只要记住「recipe 决定缩放粒度」即可。

#### 4.1.3 源码精读

`check_sf_layout` 负责把 recipe 翻译成「SF 张量应该长什么样」的断言，是本讲最重要的形状公式来源：

```cpp
DG_HOST_ASSERT(sf.size(-2) == ceil_div(mn, gran_mn));
DG_HOST_ASSERT(sf.size(-1) == ceil_div(k, gran_k * (sf_dtype == torch::kFloat ? 1 : 4)));
```

参见 [csrc/utils/layout.hpp:97-98](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp#L97-L98)。这两行给出 SF 形状的通用公式：

- **MN 维**（`size(-2)`）：\(\lceil \mathrm{mn} / \mathrm{gran\_mn} \rceil\)。
- **K 维**（`size(-1)`）：FP32 时是 \(\lceil k / \mathrm{gran\_k} \rceil\)；INT（已打包 UE8M0）时因为 4 个字节打包进一个 int32，再除以 4，变成 \(\lceil k / (\mathrm{gran\_k}\times 4) \rceil\)。

> 关键约定：MN 维总是「真实 MN」长度（不补齐），而补齐体现在 stride 上（见 4.3）。这使得 SF 张量在语义上和原矩阵一一对应，只在物理排布上做对齐。

#### 4.1.4 代码实践

**目标**：用纯 Python 工具亲手造一次 FP32 SF，验证它的形状与上面的公式一致。

**操作步骤**（在装有 PyTorch 的环境运行；FP8 cast 需要 CUDA 设备，UE8M0 数学部分 CPU 亦可）：

```python
import torch
from deep_gemm.utils import ceil_div
from deep_gemm.utils.math import per_token_cast_to_fp8

M, K, gran_k = 4096, 7168, 128
x = torch.randn((M, K), device='cuda', dtype=torch.bfloat16)

# 不使用 UE8M0：得到原生 FP32 SF（SM90 风格）
x_fp8, sf = per_token_cast_to_fp8(x, use_ue8m0=False, gran_k=gran_k)
print('FP32 SF:', sf.shape, sf.dtype)   # 预期: [4096, 56]  torch.float32
assert sf.shape == (M, ceil_div(K, gran_k))
```

**需要观察的现象**：`sf.shape` 应为 `[4096, 56]`（因为 `7168 / 128 = 56`），`dtype` 为 `torch.float32`。

**预期结果**：断言通过，证明「K 维 = ceil(K/gran_k)」成立。

> 注意：`per_token_cast_to_fp8` 在内部会把 K 补齐到 `gran_k` 的倍数（`padded_n = align(n, gran_k)`），因此即使 `K` 不是 `gran_k` 的整数倍也能工作，SF 形状仍由 `ceil_div` 决定。如果 `K % gran_k != 0`，请用 `ceil_div(K, gran_k)` 而非 `K // gran_k`。

#### 4.1.5 小练习与答案

**练习 1**：给定 `recipe = (1, 128, 128)`、`A` 形状 `[M, K] = [4096, 7168]`、`B` 形状 `[N, K] = [5120, 7168]`，求 SFA 与 SFB（FP32）的形状。

> **答案**：SFA 的 `gran_mn_a = 1`，故 MN 维 = `4096`，K 维 = `ceil(7168/128) = 56`，形状 `[4096, 56]`。SFB 的 `gran_mn_b = 128`，故 MN 维 = `ceil(5120/128) = 40`，K 维 = `56`，形状 `[40, 56]`。这正是「A 用 `per_token_cast_to_fp8`、B 用 `per_block_cast_to_fp8`」的原因。

**练习 2**：为什么 `gran_k` 通常取 128 而不是 1？

> **答案**：`gran_k=1` 意味着每个 K 元素一个 SF，SF 数量与原张量一样多，省不了显存/带宽，毫无意义；`gran_k=128` 让一个 SF 恰好覆盖 tensor core 一次 MMA 在 K 方向吃掉的 128 个元素，既显著减少 SF 体积，又与硬件计算粒度对齐、便于在 kernel 内高效应用。

---

### 4.2 FP32 vs UE8M0 打包

#### 4.2.1 概念说明

同样的 SF 数值，在两代架构上用完全不同的格式存放，这是本讲的核心差异：

- **SM90（Hopper）**：SF 用 **FP32**（`torch.float32`）存储。kernel 在累加时自己把 FP32 比例尺乘回去。
- **SM100（Blackwell）**：SF 用**打包 UE8M0** 存储。UMMA（SM100 的 tensor core 指令）**硬件原生支持 UE8M0 缩放**，能在一次乘加里直接吸收比例尺，因此软件要把 FP32 比例尺预先转成 UE8M0 格式。

什么是 UE8M0？它只存一个 8 位无符号指数 \(E\)，对应的浮点值是

\[
\mathrm{value} = 2^{\,E - 127}
\]

（127 是 FP32 的指数偏置）。因为尾数为 0，UE8M0 **只能表示 2 的整数次幂**，精度比 FP32 粗很多。但它有两个好处：

1. 只占 1 字节（FP32 的 1/4），4 个还能继续打包进一个 int32。
2. SM100 硬件能直接吃，免去 kernel 里的软件乘法。

为了把任意 FP32 比例尺变成 UE8M0，必须先**向上取整到最近的 2 的幂**（`ceil_to_ue8m0`）：向上取整使 \(sf\) 偏大，于是 \(q = x/sf\) 偏小，安全落在 `[-448, 448]` 内不溢出。这是一个「牺牲一点点缩放精度换取硬件加速 + 显存节省」的取舍。

#### 4.2.2 核心流程

SM100 上 SF 从 FP32 到打包 UE8M0 的三步：

```
FP32 SF  [M, K/gran_k]  (已 ceil_to_ue8m0)
   │ ① 取指数: (sf.view(int32) >> 23) & 0xFF   → uint8, 值域 1..254
   ▼
UE8M0 字节 [M, K/gran_k]   (uint8)
   │ ② 4 个字节打包进 1 个 int32  → K 维 ÷ 4
   ▼
打包 UE8M0 [M, ceil(K/(gran_k*4))]  (int32)
   │ ③ 转置成 MN-major + 16 字节对齐 (见 4.3)
   ▼
kernel 所需布局
```

对应代码 `ceil_to_ue8m0` 与 `pack_ue8m0_to_int`：

```python
def ceil_to_ue8m0(x):
    bits = x.abs().float().view(torch.int)
    exp = ((bits >> 23) & 0xFF) + (bits & 0x7FFFFF).bool().int()
    return (exp.clamp(1, 254) << 23).view(torch.float)

def pack_ue8m0_to_int(x):
    x_int = x.view(torch.int)
    return (x_int >> 23).to(torch.uint8).view(torch.int)
```

参见 [deep_gemm/utils/math.py:13-23](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/math.py#L13-L23)。

- `ceil_to_ue8m0`：`(bits >> 23) & 0xFF` 取出指数；`+ (bits & 0x7FFFFF).bool().int()` 表示「尾数非零就把指数再加 1」，正好实现「向上取整到 2 的幂」；`clamp(1, 254)` 保证结果是合法的非零有限 FP32。
- `pack_ue8m0_to_int`：再次右移 23 位得到 1 字节指数，view 成 int 后每 4 个连续字节天然落在一个 int32 里。

C++ 侧 `get_mn_major_tma_aligned_packed_ue8m0_tensor_torch` 用同样的位运算做参考实现，可作为「黄金答案」：

```cpp
const auto ue8m0_tensor = sf_reshaped.view(torch::kInt32).bitwise_right_shift(23).to(torch::kUInt8);
// ... pad 到对齐, view 成 int32, 再转置 ...
```

参见 [csrc/jit_kernels/impls/smxx_layout.hpp:155-178](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_layout.hpp#L155-L178)。真实库里性能更优的 CUDA 版本是 `get_mn_major_tma_aligned_packed_ue8m0_tensor`（[smxx_layout.hpp:180-253](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_layout.hpp#L180-L253)），torch 版只是它的可读参考。

#### 4.2.3 源码精读

逆操作 `unpack_ue8m0_from_int` 能帮我们确认 UE8M0 的语义：

```python
def unpack_ue8m0_from_int(packed_sf):
    return (packed_sf.view(torch.uint8).to(torch.int) << 23).view(torch.float)
```

参见 [deep_gemm/utils/math.py:137-138](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/math.py#L137-L138)。把 1 字节指数左移回 23 位、零尾数，还原成「2 的整数次幂」的 FP32。这一个 `<< 23` 清楚地展示了 UE8M0 字节就是 FP32 的高 8 位指数域。

格式差异最终体现在 README 的权威说明里（[README.md:67-70](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L67-L70)）：

- SM90 requires scaling factors in **FP32** format.
- SM100 requires scaling factors in **packed UE8M0** format, which packs 4 UE8M0 into a single `torch.int`.

#### 4.2.4 代码实践

**目标**：亲手做一次 UE8M0 打包/解包往返（round-trip），验证「向上取整到 2 的幂」与「4 打 1」两个性质。此实践只依赖纯 Python 工具，CPU 即可运行。

**操作步骤**：

```python
import torch
from deep_gemm.utils.math import ceil_to_ue8m0, pack_ue8m0_to_int, unpack_ue8m0_from_int

sf = torch.tensor([[0.003, 0.0031, 0.5, 1.0]], dtype=torch.float)  # [1,4]
sf_ue = ceil_to_ue8m0(sf)          # 向上取整到最近的 2 的幂
print('ceil_to_ue8m0:', sf_ue.tolist())
# 预期: 0.003->0.00390625(=2^-8), 0.0031->0.00390625, 0.5->0.5(=2^-1), 1.0->1.0(=2^0)

packed = pack_ue8m0_to_int(sf_ue)  # 4 个字节 -> 1 个 int32
print('packed shape/dtype:', packed.shape, packed.dtype)   # 预期: [1,1] int32
print('packed value (hex):', hex(packed.view(torch.uint8).tolist()[0][0]))

restored = unpack_ue8m0_from_int(packed)
assert torch.equal(sf_ue, restored)
```

**需要观察的现象**：`0.003` 与 `0.0031` 都被 ceil 成同一个 `0.00390625`（即 \(2^{-8}\)），证明 UE8M0 牺牲了尾数精度；`packed` 从 4 个 float 压成 1 个 int32；解包后与 ceil 后的值逐位相等。

**预期结果**：断言 `torch.equal(sf_ue, restored)` 通过，证明打包/解包无损（无损的是 UE8M0 表示本身，FP32→UE8M0 的精度损失发生在 `ceil_to_ue8m0` 这一步）。

> 待本地验证：具体的 packed 字节序与机器字节序有关，上面的 `hex` 值在不同环境可能不同，但「形状 `[1,1]`、dtype `int32`」是确定的。

#### 4.2.5 小练习与答案

**练习 1**：把 `sf = 3.0` 经过 `ceil_to_ue8m0` 后得到多少？为什么？

> **答案**：得到 `4.0`（\(2^2\)）。`3.0` 的 FP32 指数域是 `128`（对应 \(2^1\)）、尾数非零，所以 `exp = 128 + 1 = 129`，还原为 \(2^{129-127} = 2^2 = 4.0\)。因为 UE8M0 不能表示 3.0（不是 2 的幂），向上取整到 4.0 保证不溢出。

**练习 2**：为什么 SM100 不直接用 FP32 SF，而要绕一圈转成 UE8M0？

> **答案**：两点收益。其一，SM100 的 UMMA 指令**硬件原生支持 UE8M0 缩放**，比例尺在一次乘加里被硬件吸收，省去 kernel 里的软件乘法与寄存器占用；其二，UE8M0 只占 1 字节且 4 打 1，SF 体积缩到 FP32 的 1/4，降低显存与 TMA 带宽压力。代价是 `ceil_to_ue8m0` 引入的少量缩放精度损失，对大模型推理/训练的最终精度影响可接受。

---

### 4.3 SF 变换分支：transform_sf_into_required_layout

#### 4.3.1 概念说明

用户给 API 的 SF（比如 `per_token_cast_to_fp8` 的输出）是「自然布局」：MN 在前、K 在后、行主序连续。但 kernel 经由 TMA 搬运 SF 时要求 **MN-major（MN 方向 stride=1）且 16 字节对齐**。此外 SM100 还要把 FP32 打包成 UE8M0。

`transform_sf_into_required_layout` 就是这座桥：它读入 `(sf, mn, k, recipe, ...)`，按 `(dtype, gran_mn, gran_k, arch_major)` 走不同分支，输出「kernel 直接能用的 SF」。对用户透明——你在 u1-l4 第一次调用 GEMM 时，SF 变换就在 `fp8_fp4_gemm_nt` 内部悄悄发生了。

#### 4.3.2 核心流程

先从 recipe 解析出 `gran_mn`/`gran_k`（A、B 共用同一函数，靠 `is_sfa` 区分取哪一项），再做前置校验，最后按架构/格式四选一派发：

```
transform_sf_into_required_layout(sf, mn, k, recipe, is_sfa, disable_ue8m0_cast)
   │
   ├─ 解析 recipe → (gran_mn, gran_k)     [三元素按 is_sfa 取 gran_mn_a 或 gran_mn_b]
   ├─ check_sf_layout(...)                 [形状/类型前置断言]
   │
   ├─① (FP32, gran_mn=1, gran_k=128) @ SM90          → 转置+TMA对齐
   ├─② (FP32, gran_mn=128, gran_k=128) @ SM90        → 仅校验 SFB 连续性
   ├─③ (FP32, gran_k∈{32,128}) @ SM100               → 打包 UE8M0 + 转置 + TMA对齐
   └─④ (INT,  gran_mn=1, gran_k∈{32,128}) @ SM100    → 仅校验(已是打包 MN-major)
```

这四条分支正好覆盖「FP32 vs UE8M0」×「需要变换 vs 已是目标格式」。

#### 4.3.3 源码精读

派发主逻辑（[csrc/apis/layout.hpp:14-61](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp#L14-L61)）：

```cpp
const auto arch_major = device_runtime->get_arch_major();
// ... 解析 recipe 得到 gran_mn, gran_k ...
check_sf_layout(sf, mn, k, gran_mn, gran_k, num_groups);

// ① SM90: (FP32, 1, 128) → 转置并 TMA 对齐
if (sf.scalar_type() == torch::kFloat and gran_mn == 1 and gran_k == 128
    and (arch_major == 9 or disable_ue8m0_cast))
    return get_mn_major_tma_aligned_tensor(sf);

// ② SM90: (FP32, 128, 128) → 不变换，仅校验 SFB
if (sf.scalar_type() == torch::kFloat and gran_mn == 128 and gran_k == 128
    and (arch_major == 9 or disable_ue8m0_cast))
    return check_sf_layout(sf, mn, k, gran_mn, gran_k, num_groups, false, true, torch::kFloat);

// ③ SM100: (FP32, *, gran_k) → 打包成 UE8M0 + MN-major TMA 对齐
if (sf.scalar_type() == torch::kFloat and (gran_k == 32 or gran_k == 128) and arch_major == 10) {
    DG_HOST_ASSERT(not disable_ue8m0_cast);
    return get_mn_major_tma_aligned_packed_ue8m0_tensor(broadcasted, psum_layout);
}

// ④ SM100: (INT, 1, gran_k) → 已经是打包 UE8M0，仅校验
if (sf.scalar_type() == torch::kInt and gran_mn == 1 and (gran_k == 32 or gran_k == 128)
    and arch_major == 10)
    return check_sf_layout(sf, mn, k, gran_mn, gran_k, num_groups, true, false, torch::kInt);
```

逐分支要点：

- **分支①（SM90 的 SFA）**：调用 `get_mn_major_tma_aligned_tensor`，把行主序 FP32 SF 转成 MN-major、并把 MN 方向补齐到 16 字节对齐。注意它**不改 dtype**（仍是 float32），只重排版式。
- **分支②（SM90 的 SFB，2D 块）**：SFB 是 `gran_mn=128` 的小矩阵，本身已是目标格式，所以**只校验不搬运**——这是「`sm90_sfb_check`」分支。
- **分支③（SM100 的 FP32→UE8M0）**：这是**最常用的一条**。标准测试路径里用户用 `per_token_cast_to_fp8(use_ue8m0=True)` 产出「已 ceil 的 FP32 SF」，API 在这里一次性完成「打包 UE8M0 + 转置 + TMA 对齐」。`disable_ue8m0_cast` 为 true 时会被拒绝（断言要求其为 false）。
- **分支④（SM100 的预打包 INT）**：用户若已自行打包（如 `use_packed_ue8m0=True`，或 mega_moe / 小 head_dim 场景），传进来的是 int32，这里**只做 TMA 对齐校验**，不再重排。

> `disable_ue8m0_cast` 是「退回 FP32」的逃生开关：当它为 true 时，分支①②的条件里多了 `or disable_ue8m0_cast`，于是即便在 SM100 上也强制走 FP32 路径——这就是 u1-l4 提到的「SM100 也可用 FP32 SF」的来源。

「TMA 对齐」到底对齐到哪？看 `get_tma_aligned_size`（[csrc/utils/math.hpp:23-27](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/math.hpp#L23-L27)）：

```cpp
static int get_tma_aligned_size(const int& x, const int& element_size) {
    constexpr int kNumTMAAlignmentBytes = 16;       // TMA 要求 16 字节对齐
    return align(x, kNumTMAAlignmentBytes / element_size);
}
```

即把 MN 方向元素数补齐到「`16 / element_size`」的倍数：FP32（4 字节）补到 4 的倍数，INT32（4 字节）也补到 4 的倍数。最终 SF 张量的 stride 满足（[csrc/utils/layout.hpp:101-107](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp#L101-L107)）：

```cpp
DG_HOST_ASSERT(sf.stride(-2) == 1 or mn == 1);                              // MN 方向 stride=1 (MN-major)
DG_HOST_ASSERT(sf.stride(-1) == get_tma_aligned_size(mn, sf.element_size())); // K 方向跨对齐宽度
```

也就是说，变换后的 SF 是 **MN-major（`stride(-2)==1`）、每个 K 切片起点 16 字节对齐** 的张量。具体产出见 `get_mn_major_tma_aligned_tensor`（[smxx_layout.hpp:120-153](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_layout.hpp#L120-L153)），其 `empty_strided` 的 stride 正是 `(tma_aligned_mn * sf_k, 1, tma_aligned_mn)`——MN 是连续内维，K 跨过对齐后的 MN 宽度。

这些函数通过 `deep_gemm/utils/layout.py`（[layout.py:1-22](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/layout.py#L1-L22)）再导出到 Python，于是你能直接写 `deep_gemm.transform_sf_into_required_layout(...)` 或 `deep_gemm.get_mn_major_tma_aligned_packed_ue8m0_tensor(...)`。注意该文件对 `_C` 的导入包在 `try/except ImportError` 里——CUDA 运行时版本低于 12.1（无 TMA）时这些函数不可用，但 `set/get_mk_alignment_for_contiguous_layout` 等始终可用。

`transform_sf_into_required_layout` 在真实 GEMM 调用里如何被触发？看 `fp8_fp4_gemm_nt`（[csrc/apis/gemm.hpp:106-107](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L106-L107)）：

```cpp
const auto [sfa, sfb, gran_k_a, gran_k_b] = layout::transform_sf_pair_into_required_layout(
    a.second, b.second, m, n, k, recipe, recipe_a, recipe_b, ...);
```

它一次性把 SFA、SFB 都变换好，再据 `arch_major` 与变换后的 dtype 派发到 SM90/SM100 内核（[gemm.hpp:110-123](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L110-L123)）：SM90 期望 `sfa.scalar_type()==kFloat`，SM100 期望 `kInt`。这条派发链正是 u2-l3 要详讲的「C++ 绑定与架构派发」。

#### 4.3.4 代码实践

**目标**：验证 `transform_sf_into_required_layout` 的输出确实满足「形状符合公式、MN-major、TMA 对齐、dtype 随架构变化」。需要 SM90 或 SM100 GPU（TMA 必备）。

**操作步骤**：

```python
import torch
import deep_gemm
from deep_gemm.utils import ceil_div, get_tma_aligned_size
from deep_gemm.utils.math import per_token_cast_to_fp8
from deep_gemm import get_arch_major

M, K, gran_k = 4096, 7168, 128
arch = get_arch_major()                      # 9 (SM90) 或 10 (SM100)
use_ue8m0 = (arch == 10)                     # SM100 才用 UE8M0

x = torch.randn((M, K), device='cuda', dtype=torch.bfloat16)
_, sf = per_token_cast_to_fp8(x, use_ue8m0=use_ue8m0, gran_k=gran_k)

# 分支①/③ 都用同一个入口，内部按架构自动派发
recipe = (1, 128, 128)                       # SM90 默认；SM100 1D1D 用 (1,1,128)
out = deep_gemm.transform_sf_into_required_layout(
    sf, mn=M, k=K, recipe=recipe, is_sfa=True)

print('in  SF:', sf.shape, sf.dtype)
print('out SF:', out.shape, out.dtype)
print('strides:', out.stride())
```

**需要观察的现象与预期结果**：

- **SM90**：`out.dtype == torch.float32`；`out.shape == [4096, 56]`；`out.stride() == (1, get_tma_aligned_size(4096, 4))`，即 MN 方向 stride=1、K 方向跨过 `align(4096, 4)=4096`（4096 已是 4 的倍数）。
- **SM100**：`out.dtype == torch.int32`（已被打包）；`out.shape[-1] == ceil_div(K, gran_k*4) == ceil(7168/512) == 14`；`out.shape[-2] == 4096`；stride 仍是 MN-major。
- MN-major 体现为 `out.stride(-2) == 1`（或 `mn==1` 时退化）。

> 待本地验证：`out.shape` 在不同 `gran_k`（32/128）与是否预打包下会不同；请在自己机器上用 `get_arch_major()` 确认架构后核对上表。无 SM90/SM100 GPU 时，可退而运行 `tests/test_layout.py`（[test_layout.py:45-80](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_layout.py#L45-L80)），它用纯 PyTorch 参考实现逐位比对打包结果，是最权威的行为说明。

#### 4.3.5 小练习与答案

**练习 1**：为什么分支①（SM90 SFA）需要变换，而分支②（SM90 SFB）几乎不变换？

> **答案**：SFA 的 `gran_mn=1`，SF 是 `[M, K/128]` 的「高瘦」行主序矩阵，TMA 要它 MN-major，所以必须转置。SFB 的 `gran_mn=128`，SF 本就是 `[N/128, K/128]` 的小块矩阵，且 SM90 kernel 对 SFB 只要求「连续或转置后连续」（`sm90_sfb_check`），因此只校验、不搬运。

**练习 2**：在 SM100 上分别用 `use_packed_ue8m0=False` 与 `True` 生成 SF，再调 `transform_sf_into_required_layout`，会分别命中分支③还是④？输出 dtype 相同吗？

> **答案**：`use_packed_ue8m0=False` 时 SF 是 FP32（已 ceil），命中**分支③**，由 API 内部打包，输出 `int32`。`use_packed_ue8m0=True` 时 SF 已是 `int32`（已打包），命中**分支④**，仅做 TMA 对齐校验，输出仍是 `int32`。两者输出 dtype 都是 `int32`，但前者把打包工作交给 API（标准路径），后者由用户预先完成（mega_moe / 小 head_dim 等高级路径）。

## 5. 综合实践

把三个最小模块串起来：给定一个 `[M, K]` 的 BF16 张量，写一段程序，**同时推导并验证 SM90 与 SM100 两种架构下 SF 的形状、dtype 与布局**，最后（在真机上）确认变换前后数值能对应上。

```python
import torch
import deep_gemm
from deep_gemm.utils import ceil_div, get_tma_aligned_size
from deep_gemm.utils.math import (
    per_token_cast_to_fp8, ceil_to_ue8m0,
    pack_ue8m0_to_int, unpack_ue8m0_from_int,
)
from deep_gemm import get_arch_major

M, K, gran_k = 4096, 7168, 128
x = torch.randn((M, K), device='cuda', dtype=torch.bfloat16)

# ---- ① 用纯公式推导两种架构的 SF 规格 ----
def spec(arch):
    fp32_k = ceil_div(K, gran_k)                     # K 方向 SF 数
    packed_k = ceil_div(K, gran_k * 4)               # UE8M0 打包后 K 方向 int32 数
    if arch == 9:
        return dict(shape=(M, fp32_k), dtype=torch.float32)
    return dict(shape=(M, packed_k), dtype=torch.int32)

for arch in (9, 10):
    print(f'arch={arch} 预期 SF:', spec(arch))

# ---- ② 用 UE8M0 工具演示 SM100 的格式链（CPU 也可跑）----
# 小例子先看清 ceil_to_ue8m0：3 个非 2 的幂都被向上取整
print('ceil_to_ue8m0([0.003, 0.5, 3.0]):',
      ceil_to_ue8m0(torch.tensor([0.003, 0.5, 3.0], dtype=torch.float)).tolist())
# 再用真实 SF 演示打包：sf_fp32 形状 [M, K/gran_k]=[4096,56]，56 是 4 的倍数可直接打包
sf_fp32 = per_token_cast_to_fp8(x, use_ue8m0=True, gran_k=gran_k)[1]  # 已 ceil 的 FP32
assert sf_fp32.size(-1) % 4 == 0                                      # pack 要求 K 维是 4 的倍数
sf_int  = pack_ue8m0_to_int(sf_fp32)                                  # 4 字节 -> 1 个 int32
print('SM100 打包后 dtype:', sf_int.dtype, 'K维:', sf_int.shape[-1], '(= FP32 版的 1/4)')
# UE8M0 往返无损：unpack 出来的都是 2 的幂，再 ceil 不变
assert torch.equal(unpack_ue8m0_from_int(sf_int).view(torch.int) >> 23,
                   sf_int.view(torch.uint8).to(torch.int))

# ---- ③ 在真机上验证 transform 的输出规格（需 SM90/SM100）----
arch = get_arch_major()
use_ue8m0 = (arch == 10)
_, sf = per_token_cast_to_fp8(x, use_ue8m0=use_ue8m0, gran_k=gran_k)
recipe = (1, 128, 128) if arch == 9 else (1, 1, 128)
out = deep_gemm.transform_sf_into_required_layout(sf, mn=M, k=K, recipe=recipe, is_sfa=True)
expected = spec(arch)
assert tuple(out.shape) == expected['shape'], (out.shape, expected)
assert out.dtype == expected['dtype']
assert out.stride(-2) == 1 or M == 1                    # MN-major
print(f'arch={arch} 变换后 OK:', out.shape, out.dtype, out.stride())
```

**检查清单**：

1. 公式推导的 `spec(arch)` 是否与 `transform` 实际输出一致？
2. SM100 的打包后 K 维是否正好是 FP32 版的 1/4？
3. 变换后张量是否满足 MN-major（`stride(-2)==1`）？

**预期结果**：三处断言全部通过；SM90 输出 `[4096, 56]` float32，SM100 输出 `[4096, 14]` int32。无 GPU 时至少完成 ①② 两步（纯 Python），把 ③ 标注「待本地验证」。

## 6. 本讲小结

- FP8/FP4 数值范围窄，必须**逐块缩放**：每块算 `sf = amax/448`，配一个比例尺；`recipe=(gran_mn, gran_k)` 就是描述这个块粒度的参数组。
- SF 形状有通用公式：MN 维 \(=\lceil \mathrm{mn}/\mathrm{gran\_mn}\rceil\)；K 维 FP32 时 \(=\lceil k/\mathrm{gran\_k}\rceil\)，打包 UE8M0 时再除以 4。
- **SM90 用 FP32 SF，SM100 用打包 UE8M0（4 个打包进一个 int32）**；UE8M0 是「8 位指数、0 位尾数」，只能表示 2 的幂，由 `ceil_to_ue8m0` 向上取整得到，SM100 硬件原生支持。
- `transform_sf_into_required_layout` 按 `(dtype, gran_mn, gran_k, arch_major)` 四路派发：SM90 转置/校验，SM100 打包/校验，最终产出 **MN-major + 16 字节对齐** 的 TMA 友好布局。
- 这一切对用户透明——`fp8_fp4_gemm_nt` 内部通过 `transform_sf_pair_into_required_layout` 自动完成，并根据变换后的 dtype（SM90=float32 / SM100=int32）派发到对应内核。
- `disable_ue8m0_cast` 是「在 SM100 上强制退回 FP32 SF」的开关，对应分支①② 里的 `or disable_ue8m0_cast` 条件。

## 7. 下一步学习建议

- **u2-l3（C++ 绑定与 API 派发）**：本讲停在「SF 变换完成、dtype 确定」，下一步正是看 `fp8_fp4_gemm_nt` 如何根据 `arch_major` 与变换后 dtype 派发到 `sm90_fp8_gemm_1d1d` / `sm100_fp8_fp4_gemm_1d1d`。
- **u4-l2（TMA 描述符与 swizzle）**：本讲反复出现的「MN-major + 16 字节对齐」最终服务于 TMA 描述符的构造，那里会解释为什么这种布局能避免 bank conflict。
- **进阶源码阅读**：若想深入 SM100 的打包 kernel，可读 [smxx_layout.hpp:180-253](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_layout.hpp#L180-L253) 的高性能 CUDA 版 `transpose_and_pack_fp32_into_ue8m0`，对照本讲的 torch 参考版理解其优化点；K 轴分组的 psum 打包路径则会出现在 u7-l3。
