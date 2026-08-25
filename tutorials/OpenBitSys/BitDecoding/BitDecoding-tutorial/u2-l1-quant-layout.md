# 量化基础：k-channel 与 k-tensor 两种模式及 pack/params 张量布局

## 1. 本讲目标

学完本讲，你应该能够：

1. **默写** k-channel 与 k-tensor 两种模式下 `k_pack` / `k_params` 的四维形状差异。
2. **解释** V 为什么固定使用 tensor 量化布局，以及这个布局在缓存拼接（`torch.cat` 的维度）上留下的"证据"。
3. **理解** `pack_num = 16 / num_bits` 的打包粒度：一个 `uint16` 容器到底装了几个小整数、装的是哪些位置上的值。
4. 给定 `(batch, seqlen_k, nheads_k, d, num_bits, group_size)`，**手工推导**出全部 8 个量化缓存张量的形状，并用一段独立 PyTorch 代码验证。

本讲是纯"数据布局"课：不读 CUDA kernel，只读 Python 侧的张量分配代码。但后续所有 CUDA kernel 的索引数学都由这些布局决定——这里不牢，第五单元的 kernel 精读会处处卡壳。

## 2. 前置知识

### 2.1 KV cache 与 decode 阶段的读取模式

承接第一单元的结论：decode 阶段每生成一个 token，注意力层都要把**整个历史 KV cache 读一遍**。此时算术强度约为 1 FLOP/Byte，是典型的 memory-bound 场景。所以加速的关键不是算得更快，而是**让 KV cache 占用的字节数变少**——这就是低比特量化打包的动机。

### 2.2 线性量化（affine quantization）

把一个 FP16 浮点数 \( x \) 映射为一个 \( n \)-bit 整数 \( q \in [0, 2^n - 1] \) 的标准方法：

\[
q = \operatorname{round}\!\left(\frac{x - z}{s}\right), \qquad \hat{x} = q \cdot s + z
\]

- \( s \)（scale）：缩放系数，等于该组数值的极差除以量化级数，\( s = \frac{x_{\max} - x_{\min}}{2^n - 1} \)；
- \( z \)（zero）：零点偏移，通常取 \( z = x_{\min} \)。

一组数值共享一对 \( (s, z) \)。组越大（`group_size` 越大），参数越省，但精度越差——因为一个 scale 要同时"照顾"分布差异更大的数值。本项目中 \( s \) 与 \( z \) 的计算发生在 QPack kernel 内（第四单元精读），本讲只关心它们**存成什么形状的张量**。

### 2.3 位打包（bit-packing）

一个 4-bit 整数只占 4 个二进制位（半个字节，俗称 nibble）。如果把每个小整数都存进一个独立变量，会浪费大量空间。位打包的思路是：把 \( 16 / n \) 个 \( n \)-bit 整数并排塞进一个 16-bit 无符号整数（`uint16`）容器里——

- 4-bit：一个 `uint16` 装 \( 16/4 = 4 \) 个整数；
- 2-bit：一个 `uint16` 装 \( 16/2 = 8 \) 个整数。

于是每个 KV 元素的平均存储从 FP16 的 2 字节降到 \( n/8 \) 字节，压缩比恰为 \( 16/n \) 倍（4 倍或 8 倍）。

### 2.4 形状记号

本讲统一使用四维形状记号 \( (b, s, h, d) \)：

| 符号 | 含义 | 典型值（Llama-3.1-8B） |
|---|---|---|
| \( b \) | batch_size | 1 |
| \( s \) | seqlen_k（KV 序列长度） | 1024、3 万+ |
| \( h \) | nheads_k（KV 头数，GQA 下远小于 Q 头数） | 8 |
| \( d \) | head_dim | 128 |

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py) | **张量分配的"标准答案"**：不用加载大模型，把 8 个量化张量逐行 `torch.zeros` 出来，是学布局最干净的入口 |
| [evaluation/llama.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py) | `LlamaBitDecoding` 的 prefill 分支：模型侧同样的张量初始化，且带 k-channel / k-tensor 的 if-else 分支 |
| [evaluation/example.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py) | 量化配置的来源：命令行参数如何变成 `config.num_bits` 等字段 |
| [bit_decode/bit_decode_interface.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py) | `kvcache_pack_int` / `fwd_kvcache_int` 的参数表——这 8 个张量就是从这里进出 CUDA 的 |
| [bit_decode/models/cache_utils.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py) | `update_pack` 的 `torch.cat` 维度——布局差异最硬核的证据 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：4.1 打包容器与 `pack_num` 常量、4.2 `group_size` 与量化参数布局、4.3 K 的两种量化模式、4.4 V 的固定 tensor 布局、4.5 llama.py 的 prefill 分支。

### 4.1 打包容器：pack_num = 16 / num_bits

#### 4.1.1 概念说明

`uint16` 是打包容器，`pack_nums`（源码中的变量名，即本讲标题里的 pack_num）回答"一个容器装几个小整数"。它不是自由参数，而是由 `num_bits` 唯一决定：

\[
\text{pack\_nums} = \frac{16}{\text{num\_bits}}
\]

这个常量在仓库里出现了两次，写法完全一致：一次在 kernel 正确性测试里，一次在模型代码里。**打包是有方向的**——一个 `uint16` 里装的 pack_nums 个值，来自同一个 (head, dim) 通道的连续若干个 token（k-channel），或同一个 (token, head) 的连续若干个 dim 通道（k-tensor / V）。方向问题留到 4.3 展开。

#### 4.1.2 核心流程

以 4-bit 为例，量化打包的流水线是：

1. 取出一组 FP16 数值（group_size 个），求 max/min，算出 scale 与 zero；
2. 每个值减 zero、除 scale、四舍五入，得到 4-bit 整数（0~15）；
3. 连续 4 个 4-bit 整数移位拼进一个 `uint16`（低位在前）；
4. 写入 `k_pack` / `v_pack` 的对应位置。

存储字节数对比（每个 KV 元素）：

| dtype | 每元素字节 | 相对 FP16 压缩比 |
|---|---|---|
| FP16 | 2 | 1× |
| int4（打包后） | 0.5 | 4× |
| int2（打包后） | 0.25 | 8× |

#### 4.1.3 源码精读

test.py 顶部的量化参数区块：

[evaluation/test.py:L40-L45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L40-L45) —— 测试脚本硬编码了五个量化常量：`quant_mode = "k-channel"`、`num_bits = 4`、`pack_nums = 16 / num_bits`（= 4.0）、`group_size = 32`、`residual_block_size = 128`。注意 `16 / num_bits` 在 Python 3 里做的是**真除法**，结果是浮点数 4.0；后面所有用到它的地方都套了 `int(...)` 或 `//`，所以不影响正确性，但读代码时不要误以为它是整数。

模型侧的同一常量：

[evaluation/llama.py:L286-L290](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L286-L290) —— `LlamaAttention.__init__` 从 config 读入 `num_bits / quant_mode / group_size / residual_block_size`，并同样用 `self.pack_nums = 16 / self.num_bits` 推导打包粒度。这说明打包粒度不是配置项，而是位宽的派生量。

命令行默认值：

[evaluation/example.py:L24-L26](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L24-L26) —— `--num_bits`（默认 4）、`--quant_mode`（默认 `k-channel`）、`--group_size`（默认 None，按位宽补默认）。

[evaluation/example.py:L34-L35](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L34-L35) —— `group_size` 的默认策略：2-bit 用 32，4-bit 用 128。位宽越低，单组能容忍的分布差异越小，需要更细的分组来保精度（代价见 4.2）。

#### 4.1.4 代码实践

1. **实践目标**：建立"打包后字节数"的数量级直觉。
2. **操作步骤**：手算 \( b=2,\ s_{\text{pack}}=896,\ h=8,\ d=128 \) 的 K 缓存在三种 dtype 下的字节数（元素总数 \( = 2 \times 896 \times 8 \times 128 = 1{,}835{,}008 \）：FP16 为 3.5 MB；int4 打包后 0.875 MB；int2 打包后 0.4375 MB）。
3. **需要观察的现象**：与 seqlen 成正比、与位宽成反比；上下文越长、位宽越低，节省的绝对量越大。
4. **预期结果**：与第一单元 README 性能图中"上下文越长加速比越高"的趋势互相印证。

#### 4.1.5 小练习与答案

**练习 1**：为什么不用 `uint8` 装两个 int4，而要用 `uint16` 装四个？
**答案**：两者存储密度相同。选 `uint16` 是 CUDA 侧的实现选择：kernel 每次从全局内存搬一个 16-bit 字，就能产出 4 个（或 8 个）反量化值，配套的 LOP3 位操作也以 16/32-bit 寄存器为单位执行，访存与指令效率更高。

**练习 2**：`num_bits = 2` 时 `pack_nums` 是多少？`k_pack` 的序列维（k-channel 模式）会缩到原来的几分之一？
**答案**：`pack_nums = 8`；序列维缩为 `seqlen / 8`，即 4-bit 模式（`seqlen / 4`）的一半。

### 4.2 量化参数张量：group_size 如何决定 *_params 的形状

#### 4.2.1 概念说明

`*_pack` 装"量化后的整数"，`*_params` 装"每组一组的 \( (s, z) \)"。`group_size` 定义了**多少个原始数值共享一组参数**。关键结论（两种模式通用）：

\[
\text{params 元素个数} = \frac{b \cdot s \cdot h \cdot d}{\text{group\_size}}, \quad \text{dtype} = \text{float32}
\]

即每 group_size 个 KV 元素对应 `*_params` 里的一个 fp32 元素。CUDA 侧会把这个 fp32 重新解释成两个 half 分别当 scale 与 zero 使用（kernel 里对 params 视图做 `cute::recast<half>`，见 [csrc/bit_decode/src/include/dequantize.h:L427-L428](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L427-L428) 的提示；精确编码方式在第四单元读 qpack.h 时严格确认）。

由此可算出 params 相对 packed 数据的**带宽开销**：

\[
\text{开销} = \frac{4 \text{ 字节}}{\text{group\_size} \times \text{num\_bits} / 8 \text{ 字节}} = \frac{32}{\text{group\_size} \cdot \text{num\_bits}}
\]

| 配置 | params 开销 |
|---|---|
| 4-bit, group 128 | 6.25% |
| 4-bit, group 32 | 25% |
| 2-bit, group 32 | 50% |

2-bit + group 32 的开销高达 50%——这就是低比特量化的隐藏成本，也是 group_size 不能无限调小的原因。

#### 4.2.2 核心流程

params 张量的维度变化规则（以 k-channel 为例）：

```
k_pack   序列维 = seqlen_pack / pack_nums     ← 按"每 pack_nums 个值一个容器"收缩
k_params 序列维 = seqlen_pack / group_size     ← 按"每 group_size 个值一组参数"收缩
其余维度 (b, h, d) 原样保留
```

一个重要约束：`seqlen_pack` 必须能被 `pack_nums` 和 `group_size` 整除，否则 `//` 除法会丢尾巴。residual 对齐机制恰好保证了这一点（见下一讲）：进入打包区的序列长度恒为 `residual_block_size`（128 或 256）的整数倍，而 128/256 同时是 `group_size`（128/32）与 `pack_nums`（4/8）的公倍数。

#### 4.2.3 源码精读

[evaluation/test.py:L77-L82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L77-L82) —— 四行分配代码就是布局的全部"官方定义"。注意第 2 维的两种收缩（`// pack_nums` 与 `// group_size`），以及 K 系列与 V 系列在**后两个维度上的不对称**（4.3、4.4 展开）：

```python
k_pack   = torch.zeros((batch_size, int(seqlen_k_pack // pack_nums), nheads_k, d),  dtype=torch.uint16, ...)
k_params = torch.zeros((batch_size, int(seqlen_k_pack // group_size), nheads_k, d), dtype=torch.float32, ...)
v_pack   = torch.zeros((batch_size, seqlen_k_pack, nheads_k, int(d // pack_nums)),  dtype=torch.uint16, ...)
v_params = torch.zeros((batch_size, int(d // group_size), nheads_k, seqlen_k_pack), dtype=torch.float32, ...)
```

这 8 个（含 `*_new`）张量随后经 [bit_decode/bit_decode_interface.py:L12-L19](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L12-L19) 的 `kvcache_pack_int` 参数表进入 CUDA——Python 层对它们的内容**不做任何加工**，只负责分配和拼接。

#### 4.2.4 代码实践

1. **实践目标**：用开销公式评估三档量化配置。
2. **操作步骤**：对 4-bit/g128、4-bit/g32、2-bit/g32 三档，分别计算 params 元素个数（以 \( b=2, s=896, h=8, d=128 \)，元素总数 1,835,008 为例：分别除以 128、32、32 得 14,336、57,344、57,344 个 fp32）。
3. **需要观察的现象**：group_size 从 128 降到 32 时，params 字节数从 56 KB 涨到 224 KB；2-bit/g32 下 params 相当于 packed 数据的一半。
4. **预期结果**：理解 example.py 中 2-bit 默认 group 32 是"精度必须"与"带宽开销"的折中，而非随意取值。

#### 4.2.5 小练习与答案

**练习 1**：若把 group_size 从 128 改成 64（4-bit），`k_params`（k-channel）形状怎么变？带宽开销变成多少？
**答案**：序列维从 `s/128` 变为 `s/64`（翻倍）；开销 = 32/(64×4) = 12.5%。第七单元 u7-l3 会实践"为 group_size=64 打通编译链路"，届时会遇到这行形状代码对应的模板分支。

**练习 2**：`v_params` 的第 1 维是 `d // group_size` 而不是 `seqlen // group_size`，这说明 V 的分组方向是什么？
**答案**：V 的 group 沿 head_dim 方向切——同一个 (token, head) 的连续 group_size 个 dim 通道共享一组 (scale, zero)；序列维被完整保留（只是挪到了最后一维）。

### 4.3 K 的两种量化模式：k-channel 与 k-tensor

#### 4.3.1 概念说明

`quant_mode` 只影响 **K** 的布局（V 恒为 tensor 布局，见 4.4）。两种模式的本质区别是**打包方向与分组方向互换**：

| | k-channel（逐通道） | k-tensor（逐张量） |
|---|---|---|
| `k_pack` 形状 | \( (b,\ s/\text{pack\_nums},\ h,\ d) \) | \( (b,\ s,\ h,\ d/\text{pack\_nums}) \) |
| `k_params` 形状 | \( (b,\ s/\text{group\_size},\ h,\ d) \) | \( (b,\ d/\text{group\_size},\ h,\ s) \) |
| 打包方向 | 沿**序列**：一个 uint16 装 4 个连续 token 在同一 (h,d) 通道的值 | 沿**通道**：一个 uint16 装同一 (token,head) 的 4 个连续 dim 值 |
| 分组方向 | 沿**序列**：连续 group_size 个 token（同一通道）共享 (s,z) | 沿**通道**：同一 token 的连续 group_size 个 dim 共享 (s,z) |
| 一个 uint16 的邻居 | 同通道、相邻 token | 同 token、相邻通道 |

注意两点：

1. **params 元素总个数两种模式完全相同**（都是 \( b \cdot s \cdot h \cdot d / g \)），区别只在"哪些数值共享一组 scale"以及维度顺序。共享的数值集合不同 → 量化误差的分布不同 → 精度表现不同。
2. **k-tensor 的 `k_params` 维度顺序变了**：序列维从第 1 维挪到了最后一维 \( (b,\ d/g,\ h,\ s) \)。这不是笔误——它和 V 的 params 布局一致（见 4.4）。

#### 4.3.2 核心流程

Python 层按 `quant_mode` 字符串分流（llama.py 的 prefill 分支）：

```
if quant_mode == 'k-channel':
    k_pack   = (b, s_pack // pack_nums, h, d)          # uint16
    k_params = (b, s_pack // group_size, h, d)          # fp32
else:  # 'k-tensor'
    k_pack   = (b, s_pack, h, d // pack_nums)           # uint16
    k_params = (b, d // group_size, h, s_pack)          # fp32
v_pack   = (b, s_pack, h, d // pack_nums)               # 两种模式下相同
v_params = (b, d // group_size, h, s_pack)              # 两种模式下相同
```

该字符串最终传入 CUDA 层参与模板 dispatch（第三单元 decode_api.cpp 精读），但**张量形状完全由 Python 侧的这几行决定**——CUDA 只是按约定往里写数。

#### 4.3.3 源码精读

带 if-else 的完整版本在模型侧：

[evaluation/llama.py:L713-L722](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L713-L722) —— prefill 分支的量化张量初始化：`if self.quant_mode == 'k-channel':` 分支分配"序列维收缩"版 K 张量；`else` 分支分配"通道维收缩 + params 转置"版；V 的两行在分支之外，两种模式共用。

test.py 则是 k-channel 的硬编码版（它把 `quant_mode` 定死在第 41 行，所以没有 if-else）：

[evaluation/test.py:L77-L79](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L77-L79) —— `k_pack` / `k_params` 按 k-channel 形状分配。想跑 k-tensor 的打包测试，需要把这四行换成上面 else 分支的形状（解码路径是否支持见练习 3）。

#### 4.3.4 代码实践

1. **实践目标**：心算 k-tensor 形状，检验对"打包方向互换"的理解。
2. **操作步骤**：取 test.py 自己的配置（\( b=1,\ s=1024,\ h=32,\ d=128 \)，4-bit、group 128；1024 恰为 128 的整数倍故 residual_len=0、s_pack=1024），分别写出两种模式下 4 个张量的形状。
3. **需要观察的现象**：k-channel 下 `k_pack = (1, 256, 32, 128)`、`k_params = (1, 8, 32, 128)`；k-tensor 下 `k_pack = (1, 1024, 32, 32)`、`k_params = (1, 1, 32, 1024)`。V 两档相同：`v_pack = (1, 1024, 32, 32)`、`v_params = (1, 1, 32, 1024)`。
4. **预期结果**：k-tensor 的 K 张量形状与 V 完全一致——因为两者用了同一种布局。

#### 4.3.5 小练习与答案

**练习 1**：k-channel 模式下，`k_pack[0, 5, 3, 64]` 这个 uint16 里装的是哪 4 个原始数值？
**答案**：token 序列第 \( 5 \times 4 = 20 \) 到 23 个（即第 20、21、22、23 个 token，低位对应第 20 个），head 3、dim 64 通道上的 4 个 K 值。它属于第 \( 20 // 128 = 0 \) 组参数，参数存在 `k_params[0, 0, 3, 64]`。

**练习 2**：为什么 k-channel 叫"逐通道"——"通道"指什么？
**答案**：通道指 (head, dim) 二元组（\( h \times d \) 个通道）。k-channel 模式下每个通道拥有**独立的一套** (scale, zero)（每组一套），通道间互不影响；而 k-tensor 模式一个 scale 横跨 group_size 个通道，粒度更粗。

**练习 3**：把 test.py 的 `quant_mode` 改成 `"k-tensor"` 会发生什么？
**答案**：张量分配会按 k-tensor 形状走（需同步改 4 行分配代码），打包 kernel 有对应分支；但**解码**（split-KV）路径的 k-tensor 模板分支在当前仓库中被注释未启用（第七单元 u7-l3 的实践主题），`fwd_kvcache_int` 会落到未实例化的分支而失败。结论：k-tensor 目前只能作为阅读素材，不能端到端跑通。此点待本地验证。

### 4.4 V 的固定 tensor 布局与 update_pack 的拼接维度

#### 4.4.1 概念说明

无论 `quant_mode` 取什么值，V 永远使用 tensor 布局：

\[
v\_pack = (b,\ s,\ h,\ d/\text{pack\_nums}), \qquad v\_params = (b,\ d/\text{group\_size},\ h,\ s)
\]

"布局含义"有三层：

1. **形状层**：`v_pack` 与 k-tensor 模式的 `k_pack` 形状完全相同——打包沿 head_dim、序列维完整保留。
2. **参数层**：`v_params` 把序列维放在**最后一维** \( (b, d/g, h, s) \)，与 `v_pack` 的维度顺序不同。这意味着同一组 (scale, zero)（同一 token、同一 head 的一组通道）在 params 里是**内存连续**的，反量化时可以一次性读出。
3. **增长层**：decode 不断追加 token，缓存沿序列方向增长。`v_pack` 沿 dim 1 增长、`v_params` 沿 dim 3（最后一维）增长——两者的"追加轴"不同。

为什么 V 固定这种布局而 K 要留两种模式？直觉层面的答案：K 参与 \( S = QK^\top \)（消减维是 \( d \)），V 参与 \( O = PV \)（消减维是 \( s \)），两次 GEMM 中 Tensor Core 对操作数 fragment 的排布要求不同，布局必须让 LOP3 反量化的产物**直接对齐** MMA fragment 而无需额外搬运。严格论证需要 fragment 级推导，留待第五单元 u5-l3（LOP3 反量化）。

#### 4.4.2 核心流程

残余区攒满一块、触发缓存扩张时，`DynamicCache.update_pack` 用 `torch.cat` 把新块拼到主缓存后面。拼接维度的选择直接暴露了布局差异：

```
key_cache_pack   : cat dim=-3   # k_pack (b, s/pack, h, d)  的 dim1（打包后的序列维）
value_cache_pack : cat dim=-3   # v_pack (b, s, h, d/pack)  的 dim1（序列维）
key_cache_params : cat dim=-3   # k_params (b, s/g, h, d)   的 dim1（分组后的序列维）
value_cache_params: cat dim=-1  # v_params (b, d/g, h, s)   的 dim3（被挪到最后的序列维）
```

#### 4.4.3 源码精读

[bit_decode/models/cache_utils.py:L657-L660](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L657-L660) —— 四行 `torch.cat` 就是上一节伪代码的原文：K 系列三个缓存统一沿 `dim=-3` 拼接，唯独 `value_cache_params` 沿 `dim=-1`。如果 V 的 params 也按 \( (b, s/g, h, d) \) 存，这里本可以统一——维度顺序的特殊性正是布局设计的"化石证据"。

另一个值得注意的观察：`key_cache_params` 的 `dim=-3` 只对 **k-channel** 布局正确（k-tensor 的 `k_params` 序列维在最后一维）。这与当前仓库只启用 k-channel 解码路径的现状互相印证（u7-l3 会看到 k-tensor 分支被注释），也再次说明：**布局、缓存拼接、kernel 分支三者是绑定的一套约定**。

触发拼接的时机（完整生命周期在下一讲展开）：

[evaluation/test.py:L152-L154](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L152-L154) —— 当残余区攒满 `residual_block_size` 时，把 kernel 输出的 `*_new` 四个张量 `update_pack` 拼进主缓存并 `clear_residual`。

#### 4.4.4 代码实践

1. **实践目标**：用 `torch.cat` 亲测"拼接轴 = 布局的序列轴"。
2. **操作步骤**（CPU 即可，示例代码）：

```python
# 示例代码：验证 v_pack 与 v_params 的追加轴不同
import torch

v_pack    = torch.zeros(1, 100, 8, 32, dtype=torch.int16)   # 用 int16 代替 uint16，形状不变
v_params  = torch.zeros(1, 1, 8, 100)                        # (b, d/g, h, s)

v_pack_new   = torch.zeros(1, 128, 8, 32, dtype=torch.int16) # 一个残余块
v_params_new = torch.zeros(1, 1, 8, 128)

v_pack   = torch.cat([v_pack,   v_pack_new],   dim=-3)
v_params = torch.cat([v_params, v_params_new], dim=-1)
print(v_pack.shape, v_params.shape)
```

3. **需要观察的现象**：输出 `(1, 228, 8, 32)` 与 `(1, 1, 8, 228)`——两个张量的序列维都从 100 涨到 228，但一个在 dim1、一个在 dim3。
4. **预期结果**：与 cache_utils.py 四行 cat 的维度选择完全对应；若把 `v_params` 的 cat 改成 `dim=-3` 会得到 `(2, 1, 8, 100)`（batch 维翻倍），显然错误——这能帮你记住为什么偏偏它是 -1。

#### 4.4.5 小练习与答案

**练习 1**：`v_params` 为什么不也存成 \( (b, s/g, h, d) \) 的"k-channel 风格"形状，让四行 cat 统一？
**答案**：反量化时 V 的参数按 (token, head) 成组读取，序列维放最后使同组参数在内存中连续、可向量化加载；统一 cat 维度只是 Python 侧的便利，kernel 侧的访存效率才是首要考量。

**练习 2**：k-tensor 模式下若沿用现在的 `update_pack`，`key_cache_params` 会在哪个维度上拼接？后果是什么？
**答案**：会在 `dim=-3` 即 \( d/g \) 维上拼接：group 数从 \( d/g \) 变成 \( 2d/g \)，而序列维不变——语义完全错误。这从缓存侧再次解释了为什么 k-tensor 解码路径必须连同缓存逻辑一起改才能启用。

### 4.5 llama.py prefill 分支：从 config 到张量分配

#### 4.5.1 概念说明

test.py 是"教科书式"的裸分配；真实模型里同样的代码长在 `LlamaBitDecoding.forward` 的 **prefill 分支**（`q_len > 1`）里，且配置来自 config 对象。prefill 分支要完成四件事：

1. 先用标准 FP16 flash-attention 算出注意力输出（精度无损）；
2. 按 `quant_mode` 分配 4 个量化张量；
3. 尾部不足 `residual_block_size` 的 token 切进残余缓存，其余调 `kvcache_pack_int` 打包；
4. 为下一阶段预分配 4 个 `*_new` 输出缓冲（残余攒满时 kernel 原位写出新块）。

#### 4.5.2 核心流程

```
输入 key_states/value_states: (b, seqlen_k, h, d) FP16
  ├─ attn_output = flash_attention(query, key, value)        # 正常算输出
  ├─ residual_len = seqlen_k % residual_block_size
  ├─ seqlen_k_pack = seqlen_k - residual_len                 # 打包区长度
  ├─ 按 quant_mode 分配 k_pack/k_params/v_pack/v_params       # 4.3 的形状规则
  ├─ 尾部 residual_len 个 token → update_residual(...)        # FP16 残余缓存
  ├─ kvcache_pack_int(k_state_past, k_pack, ..., num_bits)    # 量化打包
  ├─ update_pack(k_pack, k_params, v_pack, v_params)          # 存入低比特主缓存
  └─ 预分配 k_pack_new 等 4 个缓冲（形状按 residual_block_size 推导）
```

#### 4.5.3 源码精读

[evaluation/llama.py:L705-L711](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L705-L711) —— 从 `key_states` 取出 `seqlen_k`，计算 `residual_len` 与 `seqlen_k_pack`：序列被切成"整块打包区 + 尾部残余区"两段，与 test.py L68-L70 完全同构。

[evaluation/llama.py:L724-L732](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L724-L732) —— 残余切分：`key_states[:, -residual_len:, :, :]` 取尾部进 `update_residual`，`[:, :-residual_len, :, :]` 留作打包输入。

[evaluation/llama.py:L734-L745](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L734-L745) —— 调用 `kvcache_pack_int` 后 `update_pack` 存入缓存。**注意一个细节**：这里传给 `kvcache_pack_int` 的长度参数是 `seqlen_k`（完整长度），而 test.py L102 传的是 `seqlen_k_pack`（打包区长度）——两处不一致，该参数在 CUDA 侧如何被使用（是否参与 grid 计算）到第三单元读 decode_api.cpp 时验证，此处标记**待确认**。

[evaluation/llama.py:L747-L750](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L747-L750) —— 4 个 `*_new` 缓冲的形状完全由 `residual_block_size` 推导：`k_pack_new = (b, residual_block_size // pack_nums, h, k_pack.size(-1))` 等。它们是 decode 阶段 kernel 的**输出**缓冲，攒满一块时由 [evaluation/llama.py:L681-L683](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L681-L683) 拼回主缓存（decode 分支的完整逻辑在 u6-l2 精读）。

config 的注入源头：

[evaluation/example.py:L44-L47](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L44-L47) —— 把命令行参数挂到 config 上：`config.num_bits / quant_mode / group_size / residual_block_size`，其中 `residual_block_size = 128 if num_bits == 4 else 256`。这些字段经 llama.py L286-L290 进入每个注意力层。

#### 4.5.4 代码实践

1. **实践目标**：建立"同一套形状规则在测试与模型两处重复出现"的对照意识。
2. **操作步骤**：并排打开 test.py L77-L82 与 llama.py L713-L722，逐行比对 8 个 `torch.zeros` 的形状表达式，标出仅有的两处不同（llama.py 有 k-channel/k-tensor 分支；test.py 只有 k-channel）。
3. **需要观察的现象**：除了分支，两处代码几乎逐字相同——模型侧没有额外的形状魔法。
4. **预期结果**：今后怀疑布局问题时，直接以 test.py 这 6 行为基准核对即可。

#### 4.5.5 小练习与答案

**练习 1**：`k_pack_new` 的形状为什么用 `k_pack.size(-1)` 而不直接写 `d`？
**答案**：为了同时兼容两种模式：k-channel 下 `k_pack` 最后一维是 \( d \)，k-tensor 下是 \( d/\text{pack\_nums} \)。用 `size(-1)` 透传，一行代码服务两种布局。

**练习 2**：prefill 结束时 `seqlen_k = 1000`、4-bit、`residual_block_size = 128`，主缓存 `k_pack` 的序列维和残余区长度各是多少？
**答案**：residual_len = \( 1000 \bmod 128 = 104 \)，seqlen_k_pack = 896；k_pack（k-channel）序列维 = \( 896/4 = 224 \)。残余区 104 个 FP16 token 参与精确计算，攒到 128 才量化。

## 5. 综合实践

**任务**：以 \( b=2,\ s_k=1000,\ h=8,\ d=128,\ \text{num\_bits}=4,\ \text{group\_size}=128 \)（4-bit 默认 `residual_block_size=128`）为例，手工推导两种模式下 8 个张量的形状，写成表格，再用独立 PyTorch 代码验证。

### 第一步：先自己推导（建议先不看答案）

前置计算：`pack_nums = 16/4 = 4`；`residual_len = 1000 % 128 = 104`；`seqlen_k_pack = 1000 - 104 = 896`。

### 第二步：参考答案表

| 张量 | dtype | k-channel 模式 | k-tensor 模式 |
|---|---|---|---|
| `k_pack` | uint16 | (2, **224**, 8, 128) | (2, 896, 8, **32**) |
| `k_params` | float32 | (2, **7**, 8, 128) | (2, **1**, 8, **896**) |
| `v_pack` | uint16 | (2, 896, 8, 32) | (2, 896, 8, 32) |
| `v_params` | float32 | (2, 1, 8, 896) | (2, 1, 8, 896) |

关键数字：\( 896/4 = 224 \)（pack 收缩）、\( 896/128 = 7 \)（group 收缩）、\( 128/128 = 1 \)（V 的 params 只有 1 组）、\( 128/4 = 32 \)（通道打包收缩）。加粗处是两种模式的差异位。V 的两行在两种模式下完全相同——这就是"V 恒为 tensor 布局"。

### 第三步：代码验证（CPU 即可运行）

```python
# 示例代码：仅用 torch.zeros 验证布局推导，无需 GPU
import torch

b, seqlen_k, h, d = 2, 1000, 8, 128
num_bits, group_size = 4, 128
pack_nums = int(16 / num_bits)        # 4
residual_block_size = 128             # 4-bit 的默认对齐块

residual_len = seqlen_k % residual_block_size     # 104
seqlen_k_pack = seqlen_k - residual_len           # 896
print(f"residual_len={residual_len}, seqlen_k_pack={seqlen_k_pack}")

for mode in ("k-channel", "k-tensor"):
    if mode == "k-channel":
        k_pack   = torch.zeros(b, seqlen_k_pack // pack_nums, h, d, dtype=torch.int16)
        k_params = torch.zeros(b, seqlen_k_pack // group_size, h, d)
    else:
        k_pack   = torch.zeros(b, seqlen_k_pack, h, d // pack_nums, dtype=torch.int16)
        k_params = torch.zeros(b, d // group_size, h, seqlen_k_pack)
    v_pack   = torch.zeros(b, seqlen_k_pack, h, d // pack_nums, dtype=torch.int16)
    v_params = torch.zeros(b, d // group_size, h, seqlen_k_pack)
    print(f"[{mode}] k_pack{tuple(k_pack.shape)} k_params{tuple(k_params.shape)} "
          f"v_pack{tuple(v_pack.shape)} v_params{tuple(v_params.shape)}")
```

（若你的 PyTorch 版本不支持 `uint16` 的 `zeros`，用 `int16` 代替即可——形状与 dtype 无关，源码里用的是 `uint16`。）

预期输出：

```
residual_len=104, seqlen_k_pack=896
[k-channel] k_pack(2, 224, 8, 128) k_params(2, 7, 8, 128) v_pack(2, 896, 8, 32) v_params(2, 1, 8, 896)
[k-tensor]  k_pack(2, 896, 8, 32)  k_params(2, 1, 8, 896)  v_pack(2, 896, 8, 32) v_params(2, 1, 8, 896)
```

### 第四步：延伸观察

- 顺手验证 `*_new` 家族（k-channel，如 test.py L110-L113）：`k_pack_new = (2, 128//4=32, 8, 128)`、`k_params_new = (2, 128//128=1, 8, 128)`、`v_pack_new = (2, 128, 8, 32)`、`v_params_new = (2, 1, 8, 128)`。
- 计算 K 的主缓存量：packed 917,504 B + params 57,344 B ≈ 0.93 MB，对照 FP16 的 3.5 MB，压缩约 3.9×（params 开销 6.25% 拉低了理论 4×）。

## 6. 本讲小结

- **pack_num = 16/num_bits**：一个 `uint16` 装 4 个 int4（或 8 个 int2），每元素存储从 2 字节降到 0.5/0.25 字节，这是 decode 提速的物理基础。
- **K 有两种模式，差异是打包/分组方向互换**：k-channel 沿序列打包、params 为 \( (b, s/g, h, d) \)；k-tensor 沿通道打包、params 为 \( (b, d/g, h, s) \)。两者 params 元素个数相同（\( b \cdot s \cdot h \cdot d / g \)），共享 scale 的数值集合不同。
- **V 恒为 tensor 布局**：`v_pack = (b, s, h, d/pack)`、`v_params = (b, d/g, h, s)`，与 quant_mode 无关；params 把序列维放最后一维，使同组参数内存连续。
- **update_pack 的四个 cat 维度是布局的硬证据**：K 系列沿 `dim=-3` 追加、`value_cache_params` 沿 `dim=-1` 追加；且该实现按 k-channel 设计，与"k-tensor 解码分支未启用"的现状互洽。
- **params 带宽开销 = 32/(group_size × num_bits)**：4-bit/g128 仅 6.25%，2-bit/g32 高达 50%——group_size 是精度与带宽的折中旋钮。
- **residual 对齐同时保证了整除约束**：`seqlen_pack` 恒为 residual_block_size（128/256）的倍数，自然被 pack_nums 与 group_size 整除。

## 7. 下一步学习建议

下一讲 **u2-l2（残余机制）**：本讲反复出现的 `residual_len`、`update_residual`、`residual_block_size` 将被完整展开——为什么最近 token 必须留在 FP16、攒满一块后何时由 kernel 原位再量化拼回主缓存（test.py L152-L154 的触发时机）。之后再进 **u2-l3（Python 接口精读）**，把 `kvcache_pack_int` / `fwd_kvcache_int` 的完整参数表与本讲的 8 个张量一一对应。若你急于知道 `k_params` 里 scale/zero 的精确编码，可跳读第四单元 u4-l3 的 qpack.h 精读，再回到主线。
