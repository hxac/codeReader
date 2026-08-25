# Python 接口精读：kvcache_pack_int 与 fwd_kvcache_int

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐参数说出 `kvcache_pack_int` 与 `fwd_kvcache_int` 两个 Python 入口的完整参数表，以及每个参数的形状、 dtype 与语义。
2. 理解 `fwd_kvcache_int` 返回的 5 元组：`out_bit` 是注意力输出，`k_pack_new` 等 4 个张量是「残余区攒满一块后新量化结果」的输出缓冲。
3. 追踪 `num_bits` 如何在 Python 层分流到 `bit_decode_cuda.fwd_kvcache_int4 / fwd_kvcache_int2` 等四个 pybind11 绑定函数，并理解它最终成为 C++ 模板参数（编译期常量）而非运行时参数。
4. 能独立编写一个不加载大模型的最小脚本：打包 → 多轮 decode → 与 FP16 参考实现对比误差。

本讲是纯 Python 层的「接口课」：我们不进入 CUDA kernel 内部（那是第四、五单元的任务），但要把「调用 kernel 前后，张量在 Python 手里经历了什么」彻底讲清。

## 2. 前置知识

阅读本讲前，请确认你已理解前几讲建立的以下概念（不熟悉的请先回顾 u2-l1 与 u2-l2）：

- **pack_nums 与打包容器**：一个 `uint16`（16 bit）装下 \(16/\text{num\_bits}\) 个量化整数——4-bit 时装 4 个（`pack_nums=4`），2-bit 时装 8 个（`pack_nums=8`）。
- **两种量化模式**：K 支持 `k-channel`（沿序列维打包，`k_pack` 形状为 `(b, s/pack_nums, h, d)`）与 `k-tensor`（沿通道维打包）；V 恒为 tensor 布局（`v_pack` 为 `(b, s, h, d/pack_nums)`）。
- **group_size 与 params 张量**：每 `group_size` 个元素共享一组 fp32 的 scale/zero，存放在 `k_params / v_params` 中。
- **残余机制**：最近的 token 以 FP16 存在残余缓存中精确参与计算，攒满一个 `residual_block_size`（4-bit 为 128）后才量化拼回主缓存。

另外补充三个本讲会用到的工程概念：

| 术语 | 通俗解释 |
|---|---|
| **out 参数（输出型参数）** | Python 函数通常用 `return` 返回结果；这里 instead 由调用方预先 `torch.zeros/torch.empty` 分配好输出张量，作为参数传入，C++ kernel 直接往里写。好处是输出缓冲可以跨轮复用，避免反复分配显存。 |
| **pybind11** | 一个 C++ 头文件库，用来把 C++ 函数包装成 Python 可调用的模块。本项目编译出的扩展模块名就叫 `bit_decode_cuda`。 |
| **模板参数 vs 运行时参数** | C++ 模板参数（如 `mha_fwd_kvcache<4>` 里的 4）在**编译期**确定，编译器可据此展开特化代码；运行时参数（如 `group_size`）在**运行期**传入，靠 if 链选择分支。`num_bits` 走的是前一条路——所以 Python 层必须先分流。 |

还有一个贯穿本讲的量化开销账本。设每个元素的原始开销为 FP16 的 16 bit，量化后：

\[
\text{每元素有效比特} \;=\; \text{num\_bits} \;+\; \frac{32}{\text{group\_size}}
\]

其中第二项是 params 的分摊开销（每组 `group_size` 个元素共享 2 个 fp32 标量，即 64 bit ÷ group_size；对 k-channel 的逐 dim 分组退化为每元素 32/group_size bit 的 scale/zero 之一，另一项由布局吸收）。例如 4-bit、group_size=32 时有效比特为 \(4+1=5\)，相对 FP16 压缩 \(16/5 = 3.2\) 倍；group_size=128 时有效比特 \(4.25\)，压缩约 3.76 倍。**group_size 越大越省带宽，但量化粒度越粗、误差越大**——这就是接口里那个看似不起眼的 `group_size` 参数背后的权衡。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `bit_decode/bit_decode_interface.py` | 唯一的 Python 接口层，全文仅 108 行 | 两个入口函数的参数表、reshape、num_bits 分流 |
| `bit_decode/__init__.py` | 包门面 | 对外导出哪两个函数 |
| `csrc/bit_decode/decode_api.cpp` | pybind11 绑定层（C++） | 4 个导出函数的签名、模板参数 `num_bits`、被固定掉的 6 个尾部参数 |
| `evaluation/test.py` | kernel 正确性测试 | 两个接口的标准调用姿势（本讲的「使用说明书」） |
| `bit_decode/models/cache_utils.py` | 改造版 DynamicCache | `update_residual / update_pack` 在调用序列中的位置（详见 u2-l2） |

## 4. 核心概念与源码讲解

### 4.1 模块一：kvcache_pack_int —— prefill 量化打包入口

#### 4.1.1 概念说明

`kvcache_pack_int` 只在 **prefill（预填充）阶段**被调用一次。此时模型刚处理完长 prompt，手上有一份完整的 FP16 KV cache；这个函数把它量化、打包，写进四个低比特张量（`k_pack / k_params / v_pack / v_params`），之后 decode 阶段反复读取的就是这份压缩缓存。

它解决的问题是：**把「FP16 的、按 token 排列的」KV，变成「低比特的、按打包容器排列的」KV**。注意两点设计：

1. **它没有返回值**——四个 pack/params 张量是调用方预分配好的 out 参数，kernel 原位写入。这允许模型层（`llama.py`）把张量分配一次、挂在 cache 对象上长期复用。
2. **它不做残余区切分**——调用方需先把序列尾部不足一个 `residual_block_size` 的 token 摘出去存 FP16，只把「对齐后的整数块」交给它（见 `test.py` 的调用姿势）。

#### 4.1.2 核心流程

```text
kvcache_pack_int(k_cache, k_pack, k_params, v_cache, v_pack, v_params,
                 opt_block_table, cu_seqlens_k, seqlen_k, quant_mode,
                 group_size, num_bits)
  │
  ├─ 1. 从 k_cache.shape 读出 (batch_size, seqlen_k, nheads_k, d)
  ├─ 2. reshape： (b, s, h, d) ──► (b*s, h, d)     # 展平成 varlen 风格，无数据拷贝
  ├─ 3. 按 num_bits 分流：
  │      num_bits == 4 ──► bit_decode_cuda.kvcache_pack_int4(...)
  │      num_bits == 2 ──► bit_decode_cuda.kvcache_pack_int2(...)
  │      其他          ──► raise ValueError
  └─ 4. C++ 侧：kvcache_qpack<4 或 2>(...) ──► set_params_fprop_qpack ──► run_kvcache_qpack ──► GPU
```

第 2 步的 reshape 值得注意：`(b, s, h, d) → (b*s, h, d)` 对连续张量只是一次**视图变换**（view），不搬运数据；真正的序列边界信息改由 `cu_seqlens_k`（累积序列长度，如 `[0, 1000, 2000]` 表示两条各 1000 长的序列）承载，C++ 侧正是用 `cu_seqlens_k.numel() - 1` 反推 batch_size 的。

#### 4.1.3 源码精读

先看导入。接口文件在导入 torch **之后**才导入编译扩展，注释写明了顺序约束：

[bit_decode/bit_decode_interface.py:8-10](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L8-L10)——`# We need to import the CUDA kernels after importing torch`，随后 `import bit_decode_cuda as bit_decode_cuda`。这个模块名就是 `setup.py` 中 `CUDAExtension` 编译出的扩展名（见 u1-l2）。

函数签名与默认值：

[bit_decode/bit_decode_interface.py:12-19](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L12-L19)——定义 `kvcache_pack_int`，12 个参数。参数表如下：

| 参数 | 类型/形状 | 语义 |
|---|---|---|
| `k_cache` | `(b, s, h, d)` fp16 | 待量化的 FP16 K |
| `k_pack` / `k_params` | 见 u2-l1 布局表，uint16 / fp32 | **输出**：K 的打包值与量化参数 |
| `v_cache` | `(b, s, h, d)` fp16 | 待量化的 FP16 V |
| `v_pack` / `v_params` | tensor 布局 | **输出**：V 的打包值与量化参数 |
| `opt_block_table` | 可选 int32 | paged-KV 页表，本项目传 `None` |
| `cu_seqlens_k` | `(b+1,)` int32 | 每条序列的累积长度（按打包区长度） |
| `seqlen_k` | int | 打包区 token 数 |
| `quant_mode` | str，默认 `"k-tensor"` | `"k-channel"` 或 `"k-tensor"` |
| `group_size` | int，默认 128 | 量化分组大小 |
| `num_bits` | int，默认 4 | 量化位宽，仅接受 2 或 4 |

展开与分流：

[bit_decode/bit_decode_interface.py:21-24](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L21-L24)——从 `k_cache.shape` 解出四维，并把 K、V 各自 reshape 成 `(b*s, h, d)` 的 `K_unpad / V_unpad`。

[bit_decode/bit_decode_interface.py:26-45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L26-L45)——`num_bits` 的 if/elif 分流：等于 4 调 `kvcache_pack_int4`，等于 2 调 `kvcache_pack_int2`，其余直接 `raise ValueError`。注意两次调用传的是同一套实参，**唯一的差别是函数名后缀**——因为 `num_bits` 在 C++ 侧是模板参数，只能靠不同入口函数承载。

标准调用姿势来自 `test.py`：

[evaluation/test.py:68-75](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L68-L75)——先算 `residual_len = seqlen_k % residual_block_size`，把序列切出对齐的 `seqlen_k_pack`，再构造 `cu_seqlens_k = [0, seqlen_k_pack, 2*seqlen_k_pack, ...]`。

[evaluation/test.py:78-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L78-L82)——按 u2-l1 的布局表 `torch.zeros` 出四个输出张量（K 系沿序列压缩、V 系沿通道压缩）。

[evaluation/test.py:87-107](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L87-L107)——调用前先把尾部残余 token 摘进 `update_residual`，再调 `kvcache_pack_int(...)`（传 `None` 作 block_table），最后 `update_pack` 把四个张量登记进 cache。这就是「prefill 一次打包，decode 反复使用」的完整准备动作。

#### 4.1.4 代码实践

**实践目标**：不跑注意力，只验证「打包是原地写入 + 压缩比符合手算」。

**操作步骤**（示例代码，可在装有编译好的 `bit_decode_cuda` 的 GPU 机器上运行）：

```python
# practice_pack.py（示例代码）
import torch, math
from bit_decode import kvcache_pack_int

b, s, h, d = 1, 1024, 32, 128
num_bits, pack_nums, group_size = 4, 16 // 4, 32
k = torch.randn(b, s, h, d, device="cuda", dtype=torch.float16)
v = torch.randn(b, s, h, d, device="cuda", dtype=torch.float16)
cu = torch.arange(0, (b + 1) * s, s, dtype=torch.int32, device="cuda")

k_pack   = torch.zeros((b, s // pack_nums, h, d), dtype=torch.uint16, device="cuda")
k_params = torch.zeros((b, s // group_size, h, d), dtype=torch.float32, device="cuda")
v_pack   = torch.zeros((b, s, h, d // pack_nums), dtype=torch.uint16, device="cuda")
v_params = torch.zeros((b, d // group_size, h, s), dtype=torch.float32, device="cuda")

kvcache_pack_int(k, k_pack, k_params, v, v_pack, v_params,
                 None, cu, s, "k-channel", group_size, num_bits)

print("k_pack nonzero?", bool((k_pack != 0).any()))          # 打包确已发生
fp16_bytes = k.element_size() * k.numel()
pack_bytes = k_pack.element_size() * k_pack.numel() + k_params.element_size() * k_params.numel()
print(f"K: fp16 {fp16_bytes/1024:.0f} KB -> packed {pack_bytes/1024:.0f} KB, "
      f"ratio {fp16_bytes/pack_bytes:.2f}x (理论 {16/(num_bits+32/group_size):.2f}x)")
```

**需要观察的现象**：`k_pack nonzero?` 为 `True`（证明 out 参数被原位写入）；实测压缩比接近理论值 \(16/(4+32/32)=3.2\) 倍。

**预期结果**：ratio 打印值约为 3.2。若想看 group_size 的影响，把 `group_size` 改为 128 重新运行（该分支同样已编译启用），ratio 应升到约 \(16/4.25 \approx 3.76\)。运行数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `kvcache_pack_int` 不返回任何值，而要调用方预先分配 `k_pack` 等张量？

**答案**：这是 out 参数风格。输出张量由模型层一次性分配并挂在 `DynamicCache` 的列表上（`update_pack` 登记），prefill 打包与后续「残余攒满再拼回」共用同一份缓冲管理逻辑；同时避免每次调用重复分配显存。函数本身只负责往传入的张量里写数据。

**练习 2**：`cu_seqlens_k` 在 reshape 成 `(b*s, h, d)` 之后承担了什么信息？C++ 侧如何用它恢复 batch 维？

**答案**：reshape 丢掉了 `(b, s)` 两维的边界，`cu_seqlens_k` 以累积长度（如 `[0, 1000, 2000]`）记录每条序列在哪结束；C++ 侧在 `kvcache_qpack` 中用 `batch_size = cu_seqlens_k.numel() - 1` 反推 batch 数（见 [decode_api.cpp:635](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L635)）。这也解释了 Python 层的 `seqlen_k` 为何叫 `max_seqlen_k`——varlen 风格下各序列长度可以不同，这里取最大值。

**练习 3**：把 `num_bits=8` 传给 `kvcache_pack_int` 会发生什么？

**答案**：两个 if 分支都不命中，走到 `raise ValueError(f"Unsupported num_bits={num_bits}; expected 2 or 4")`，在 Python 层直接抛错，不会进入 C++。

### 4.2 模块二：fwd_kvcache_int —— decode 低比特注意力入口

#### 4.2.1 概念说明

`fwd_kvcache_int` 在 **decode 阶段每生成一个 token 调用一次**。它接收：单 token 的 Query、低比特打包主缓存、FP16 残余缓存（含新追加的 kv，已补零对齐到 `residual_block_size`），一次性完成「反量化 + 注意力 + 在线 softmax」并返回结果。

它的返回值是一个 **5 元组**，这是理解本函数的关键：

| 返回值 | 形状 | 用途 |
|---|---|---|
| `out_bit` | 同 `q`，fp16 | 注意力输出，直接作为该层 attention 结果 |
| `k_pack_new` | `(b, residual_block_size/pack_nums, h, d)` uint16 | **仅当残余攒满一块时有效**：kernel 内顺带量化出的新 K 打包块 |
| `k_params_new` | `(b, residual_block_size/group_size, h, d)` fp32 | 新块的 K 量化参数 |
| `v_pack_new` | `(b, residual_block_size, h, d/pack_nums)` uint16 | 新块的 V 打包值 |
| `v_params_new` | `(b, d/group_size, h, residual_block_size)` fp32 | 新块的 V 量化参数 |

注意 `*_new` 四个张量**既是输入又是输出**：调用方预先分配（`test.py` 中只分配一次），每轮传入让 kernel 写；只有当残余区恰好攒满（`new_lens == residual_block_size`）时 kernel 才写入有意义的内容，调用方据此决定是否 `update_pack` 拼回主缓存。这四个缓冲跨轮复用，避免每步 decode 都分配显存。

#### 4.2.2 核心流程

一次 decode 调用在 Python 侧的数据流（对照 `test.py` 的循环体）：

```text
每轮 decode：
  1. update_pack(None,None,None,None,idx)     # 读出主缓存 4 张量
  2. seqlens_k = full((b,), v_pack.shape[1])  # 打包区 token 数（FP16 等效长度）
  3. update_residual(k_new, v_new, idx)       # 残余区追加 1 个新 kv
  4. 残余缓存拷进固定形状的补零缓冲 k_residual / v_residual
  5. fwd_kvcache_int(q, k_pack, k_params, v_pack, v_params,
                     k_residual, v_residual, seqlens_k,
                     k_pack_new, ..., softmax_scale, quant_mode,
                     group_size, residual_block_size,
                     new_lens=cur_residual_len, num_bits)
  6. 若 cur_residual_len == residual_block_size：
        update_pack(k_pack_new, ...) + clear_residual(idx)   # 拼回主缓存并清空残余区
```

两个长度参数的语义**最容易混淆**，请务必分清：

- `opt_seqlens_k`（张量，每 batch 一个）：**打包主缓存**里的 token 数，即「已量化的旧上下文有多长」。它等于 `v_pack.shape[1]`——V 的布局沿 d 压缩，序列维保持原长，所以直接读形状即可。
- `new_lens`（标量）：**残余区 + 新 token** 的有效长度（补零之前的真实长度）。kernel 靠它知道补零缓冲里前多少行是真数据；它等于 `residual_block_size` 时触发「顺带再量化」。

两者相加才是本轮注意力的总 KV 长度。

#### 4.2.3 源码精读

函数签名（19 个参数，含 4 个 `*_new` 缓冲）：

[bit_decode/bit_decode_interface.py:47-61](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L47-L61)——`fwd_kvcache_int` 的完整参数表：`q`、四个主缓存张量、可选的 `opt_k_new / opt_v_new / opt_seqlens_k`、四个 `*_new` 输出缓冲、`opt_block_table`、`softmax_scale`（默认 1.0，一般传 \(1/\sqrt{d}\)）、`quant_mode`（默认 `"k-tensor"`）、`group_size`（默认 128）、`residual_block_size`（默认 128）、`new_lens`（默认 0）、`num_bits`（默认 4）。

int4 分支的调用与「被固定的 6 个尾部参数」：

[bit_decode/bit_decode_interface.py:63-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L63-L82)——调用 `bit_decode_cuda.fwd_kvcache_int4`，解包出 5 个返回值。注意实参列表尾部有 6 个带 `# Added` 注释的字面量：`False, -1, -1, 0.0, True, 0`。它们对应 C++ 签名里 Python 层**不打算暴露**的开关：`is_causal=False`、`window_size_left=-1`、`window_size_right=-1`、`softcap=0.0`、`is_rotary_interleaved=True`、`num_splits=0`（自动启发式）。也就是说：Python 门面把 FlashAttention 继承来的滑动窗口、softcap 等能力一律关死，只保留 decoding 所需的最小路径。

int2 分支与非法值兜底：

[bit_decode/bit_decode_interface.py:83-104](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L83-L104)——`num_bits == 2` 时调 `fwd_kvcache_int2`，实参完全同构；其余值抛 `ValueError`。两个分支唯一的差别还是绑定的目标函数。

统一返回：

[bit_decode/bit_decode_interface.py:107](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L107)——`return out_bit, k_pack_new, k_params_new, v_pack_new, v_params_new`，即上文表格中的 5 元组。

C++ 侧签名对照（用于理解那 6 个固定值落在哪里）：

[csrc/bit_decode/decode_api.cpp:317-341](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L317-L341)——`mha_fwd_kvcache` 模板函数的参数表：从 `q` 到 `new_lens` 与 Python 一一对应，随后的 `is_causal / window_size_left / window_size_right / softcap / is_rotary_interleaved / num_splits` 正是 Python 层写死的 6 个。注释还标清了 `k_ / v_`（残余区）与 `seqlens_k_` 的形状约定。

调用侧（test.py 的 decode 循环）：

[evaluation/test.py:123-135](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L123-L135)——`seqlen_pack = v_pack.shape[1]` 构造 `seqlens_k`；`update_residual` 追加新 kv 后，把残余缓存拷入 `(b, residual_block_size, h, d)` 的补零缓冲，并读出 `cur_residual_len`。

[evaluation/test.py:137-150](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L137-L150)——`fwd_kvcache_int` 的实参传法：第 6-8 位是补零后的 `k_residual, v_residual, seqlens_k`，第 9-12 位是四个 `*_new` 缓冲，最后把 `cur_residual_len` 作为 `new_lens` 传入。

[evaluation/test.py:152-154](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L152-L154)——只有 `cur_residual_len == residual_block_size` 时才把 `*_new` 拼回主缓存并清空残余区。**这是消费 5 元组的判据**：不满一块时 `*_new` 内容无意义，直接丢弃。

缓冲预分配的位置：

[evaluation/test.py:110-113](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L110-L113)——循环外用 `torch.empty` 一次性分配四个 `*_new` 缓冲，形状按「恰好一个 residual_block_size 块」的布局给出（K 系沿序列压缩、V 系沿通道压缩，与 u2-l1 的方向约定一致）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认 `seqlens_k` 与 `new_lens` 的语义差异，以及 `*_new` 只在攒满时有效。

**操作步骤**：

1. 打开 [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py)，在 [第 132 行](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L132) 后加一行打印：`print(round_idx, "seqlens_k =", seqlens_k[0].item(), "new_lens =", cur_residual_len)`。
2. 把 [第 116 行](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L116) 的循环上界改成 200，运行 `python evaluation/test.py`。
3. 在 [第 152 行](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L152) 前插入 `print("k_pack_new abs-sum:", k_pack_new.float().abs().sum().item() / 1e6)`。

**需要观察的现象**：每轮打印中 `seqlens_k` 从 1024 起步、`new_lens` 从 0 起步逐轮 +1；当 `new_lens` 到达 128 的那一轮，`k_pack_new` 的非零量显著跳变（之前接近 0 或维持旧值），随后 `seqlens_k` 跳到 1152、`new_lens` 归 0。

**预期结果**：`seqlens_k + new_lens` 逐轮 +1 且等于总上下文长度；每 128 轮出现一次「阶梯」。若看不到（例如默认配置 `seqlen_k=1024` 恰被 128 整除、残余区从 0 开始），属于正常——首个满块出现在第 128 轮之后。具体轮次编号**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`fwd_kvcache_int` 的 `opt_seqlens_k` 和 `new_lens` 分别表示什么长度？为什么一个用张量、一个用标量？

**答案**：`opt_seqlens_k` 是打包主缓存的 token 数（每 batch 一个，batch 间序列长度可不同，所以是 `(b,)` 张量）；`new_lens` 是残余区加新 token 的有效长度（补零前），全 batch 统一按 `residual_block_size` 对齐管理，所以是标量。两者之和是本轮注意力的总 KV 长度。

**练习 2**：为什么 `test.py` 在 `cur_residual_len != residual_block_size` 的轮次丢弃 `k_pack_new` 等返回值也是安全的？

**答案**：kernel 只在残余攒满一个块时才往 `*_new` 缓冲写入有效量化结果（由 `new_lens` 触发，详见第五单元的残余 kernel）；未攒满时这些缓冲内容是无意义的旧数据。判据 `cur_residual_len == residual_block_size` 正好对应 kernel 写入的条件，且缓冲下一轮会被复用覆盖，丢弃不影响正确性。

**练习 3**：`softmax_scale` 的默认值是 1.0，`test.py` 里传的是什么？为什么不直接在 kernel 里自动计算？

**答案**：`test.py` 传 `sm_scale = 1.0 / math.sqrt(d)`（[evaluation/test.py:58](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L58)）。保持显式传入是为了兼容部分模型使用非标准缩放（如某些 GPT 变体乘 \(1/\sqrt{d-1}\) 或额外的缩放因子）的惯例，接口层不做假设。

### 4.3 模块三：num_bits 的分流终点 —— bit_decode_cuda 的 int2/int4 绑定

#### 4.3.1 概念说明

Python 层的 if/elif 分流终点是编译扩展 `bit_decode_cuda` 里的**四个**导出函数。`num_bits` 在这里完成身份转换：从 Python 的运行时整数，变成 C++ 的**模板参数**。

这个设计的原因是性能：反量化路径（LOP3 位操作）对 2-bit 与 4-bit 是完全不同的指令序列，打包容器布局（`pack_nums=4` 还是 8）也随位宽改变；把这些做成编译期常量，编译器才能生成无分支的特化 kernel。代价则是**组合爆炸**：每个 (quant_mode, num_bits, group_size) 组合都需要一份显式实例化并参与编译（u1-l2 讲过 genfile 拆分编译的动机，u7-l3 将动手打通一个被注释的新组合）。

#### 4.3.2 核心流程

```text
bit_decode/__init__.py          （门面：导出 2 个函数 + 3 个 cache 类）
        │ 调用
bit_decode_interface.py          （num_bits if/elif 分流）
        │ 调用
bit_decode_cuda（pybind11 模块，4 个导出函数）
        ├─ kvcache_pack_int4 ──► kvcache_qpack<4>   （模板参数 num_bits=4）
        ├─ kvcache_pack_int2 ──► kvcache_qpack<2>
        ├─ fwd_kvcache_int4  ──► mha_fwd_kvcache<4>
        └─ fwd_kvcache_int2  ──► mha_fwd_kvcache<2>
                 │ 内部再按 quant_mode / group_size 的 if 链
                 └─► run_kvcache_qpack / run_mha_fwd ──► 具体模板实例 ──► GPU
```

#### 4.3.3 源码精读

包门面：

[bit_decode/__init__.py:1-8](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/__init__.py#L1-L8)——`__version__` 之后从 `bit_decode_interface` 导出 `kvcache_pack_int` 与 `fwd_kvcache_int`，并从 `bit_decode.models.cache_utils` 导出三个 cache 类。这就是 `test.py` 第 9 行 `from bit_decode import kvcache_pack_int, fwd_kvcache_int` 能成立的原因——用户永远不需要直接碰 `bit_decode_cuda`。

pybind11 导出点：

[csrc/bit_decode/decode_api.cpp:688-693](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L688-L693)——`PYBIND11_MODULE` 用 `m.def` 注册 4 个函数：`kvcache_pack_int2/int4` 绑定到 `kvcache_qpack<2>/<4>`，`fwd_kvcache_int2/int4` 绑定到 `mha_fwd_kvcache<2>/<4>`。**注意尖括号里的 2/4**：位宽在此刻成为编译期常量。

qpack 侧的 C++ 签名（与 Python 实参逐位对照）：

[csrc/bit_decode/decode_api.cpp:603-610](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L603-L610)——`kvcache_qpack` 的参数表里**没有 num_bits**（它是模板参数），Python 传的 `seqlen_k` 在这里叫 `max_seqlen_k`（varlen 语义）。随后 [658-670 行](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L658-L670) 的 `set_params_fprop_qpack` 把张量指针、stride 与量化配置装进 `Flash_fwd_params` 结构体（该结构体的字段详解是 u3-l2 的主题），[679-682 行](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L679-L682) 在 `max_seqlen_k > 0` 时启动 `run_kvcache_qpack`。

运行期 dispatch 与「当前启用的组合」：

[csrc/bit_decode/decode_api.cpp:220-238](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L220-L238)——`run_kvcache_qpack` 内部再按 `quant_mode` 与 `group_size` 选模板实例：只有 `k-channel` 的 `group_size == 32 / 128` 两个分支是活的，`group_size == 64` 与整个 `k-tensor` 分支全部被注释。`run_mha_fwd` 侧同理（[decode_api.cpp:199-214](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L199-L214)）。

由此得到一个**重要的实用结论**：虽然 Python 签名的默认值写着 `quant_mode="k-tensor"`，当前仓库实际能跑的调用组合是 `quant_mode="k-channel"` 且 `group_size ∈ {32, 128}`、`num_bits ∈ {2, 4}`。传入其他组合不会在 Python 层报错，但会静默地什么都不做（qpack 分支为空）——这是阅读/使用本接口时最容易踩的坑。

decode 侧的两处绑定层细节（为 u3-l1 预热）：

[csrc/bit_decode/decode_api.cpp:381](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L381)——`seqlen_q == 1` 时强制 `is_causal = false`：单 token query 的因果掩码退化为全可见，这也解释了 Python 层把 `is_causal` 固定为 `False` 是无害的。

[csrc/bit_decode/decode_api.cpp:385-391](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L385-L391)——GQA 优化：当 `seqlen_q == 1` 且 query 头数多于 KV 头数时，把 `q` 从 `(b, 1, nheads, d)` 重排为 `(b, ngroups, nheads_k, d)`，等效地把「多头共享同一份 KV」变成一个 batch 维，一次 kernel 调用处理所有 query 组。

#### 4.3.4 代码实践

**实践目标**：验证「非法组合静默失败」的论断，体会 dispatch 边界。

**操作步骤**：

1. 复用 4.1.4 的 `practice_pack.py`，把 `quant_mode` 改为 `"k-tensor"`，其余不变，运行。
2. 再改回 `"k-channel"`、`group_size=64`，运行。
3. 两种情况下都观察 `(k_pack != 0).any()` 与压缩比打印。

**需要观察的现象**：两种非法组合下 `k_pack nonzero?` 应为 `False`（kernel 未被启动，zeros 原样返回），程序**不报错**。

**预期结果**：印证 [decode_api.cpp:220-238](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L220-L238) 中被注释的分支确实不可达；这也说明 `quant_mode` 的取值合法性完全由 dispatch 代码而非 Python 类型系统保证。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `num_bits` 必须在 Python 层分流，而 `group_size` 可以作为运行时参数直接传进 C++？

**答案**：两者其实都影响 kernel 代码生成，区别在工程取舍。`num_bits` 决定反量化的指令级路径（LOP3 位段提取完全不同）与 `pack_nums` 布局，差异巨大，值得模板特化，于是拆成 `int2/int4` 两组绑定入口、编译期展开；`group_size` 只改变量化分组的边界计算，代码结构不变，用运行时 if 链选择模板实例即可，代价是每个取值也要一份实例化（所以同样只有 32/128 被启用）。

**练习 2**：`bit_decode_cuda` 里为什么恰好是 4 个导出函数，而不是 2 个？

**答案**：因为有 2 个功能（qpack 打包、fwd 解码注意力）× 2 个位宽（2/4-bit）。位宽是模板参数无法运行时传入，只能为每个 (功能, 位宽) 组合注册一个入口；Python 层的 if/elif 把这两个维度重新合并成带 `num_bits` 参数的统一接口。

**练习 3**：如果让你给 `fwd_kvcache_int` 增加 `is_causal` 暴露给用户，最小改动是什么？

**答案**：在 [bit_decode/bit_decode_interface.py:47-61](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L47-L61) 的签名里加 `is_causal: bool = False`，并把两个分支中写死的字面量 `False` 替换为该变量即可——C++ 签名本来就接受这个参数（[decode_api.cpp:335](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L335)），无需重新编译扩展。但注意 `seqlen_q == 1` 时它会被 C++ 侧强制置 false。

## 5. 综合实践

把本讲三个模块串成一个完整的最小验证脚本（示例代码）。目标：**不加载任何大模型，仅用随机张量走通「打包 → 5 轮 decode → 对比 FP16 参考」的完整闭环**，作为后续阅读 CUDA kernel 时的行为基线。

```python
# u2l3_minimal.py（示例代码）
import torch, math
from bit_decode import kvcache_pack_int, fwd_kvcache_int
from bit_decode import DynamicCache

# ---- 配置：与 test.py 一致，落在已启用的 dispatch 组合内 ----
quant_mode, num_bits, group_size = "k-channel", 4, 32
pack_nums, residual_block_size = 16 // num_bits, 128
b, h, d = 1, 32, 128
seqlen_k = 1024
sm_scale = 1.0 / math.sqrt(d)
device, dtype = "cuda", torch.float16
layer_idx = 0

def attention_ref(q, k, v):                      # 摘自 evaluation/test.py:13-37
    scores = torch.einsum("bthd,bshd->bhts", q / math.sqrt(d), k)
    attn = torch.softmax(scores, dim=-1).to(v.dtype)
    return torch.einsum("bhts,bshd->bthd", attn, v).to(q.dtype)

torch.manual_seed(42)
q = torch.rand(b, 1, h, d, device=device, dtype=dtype)
k_state = torch.randn(b, seqlen_k, h, d, device=device, dtype=dtype)
v_state = torch.randn(b, seqlen_k, h, d, device=device, dtype=dtype)

# ---- 模块一：prefill 打包（对照 test.py:61-113）----
residual_len = seqlen_k % residual_block_size
seqlen_k_pack = seqlen_k - residual_len
cu_seqlens_k = torch.arange(0, (b + 1) * seqlen_k_pack, seqlen_k_pack,
                            dtype=torch.int32, device=device)
k_pack   = torch.zeros((b, seqlen_k_pack // pack_nums, h, d), dtype=torch.uint16, device=device)
k_params = torch.zeros((b, seqlen_k_pack // group_size, h, d), dtype=torch.float32, device=device)
v_pack   = torch.zeros((b, seqlen_k_pack, h, d // pack_nums), dtype=torch.uint16, device=device)
v_params = torch.zeros((b, d // group_size, h, seqlen_k_pack), dtype=torch.float32, device=device)

cache = DynamicCache()
k_past, v_past = k_state[:, :-residual_len], v_state[:, :-residual_len] if residual_len else (k_state, v_state)
if residual_len:
    cache.update_residual(k_state[:, -residual_len:], v_state[:, -residual_len:], layer_idx)
kvcache_pack_int(k_past, k_pack, k_params, v_past, v_pack, v_params,
                 None, cu_seqlens_k, seqlen_k_pack, quant_mode, group_size, num_bits)
cache.update_pack(k_pack, k_params, v_pack, v_params, layer_idx)

# ---- *_new 输出缓冲：一次分配、轮次复用（对照 test.py:110-113）----
k_pack_new   = torch.empty((b, residual_block_size // pack_nums, h, k_pack.size(-1)), dtype=torch.uint16, device=device)
k_params_new = torch.empty((b, residual_block_size // group_size, h, k_params.size(-1)), dtype=torch.float32, device=device)
v_pack_new   = torch.empty((b, residual_block_size, h, v_pack.size(-1)), dtype=torch.uint16, device=device)
v_params_new = torch.empty((b, v_params.size(1), h, residual_block_size), dtype=torch.float32, device=device)

# ---- 模块二：5 轮 decode（对照 test.py:116-160）----
for step in range(5):
    k_new = torch.randn(b, 1, h, d, device=device, dtype=dtype)
    v_new = torch.randn(b, 1, h, d, device=device, dtype=dtype)
    k_pack, k_params, v_pack, v_params = cache.update_pack(None, None, None, None, layer_idx)
    seqlens_k = torch.full((b,), v_pack.shape[1], dtype=torch.int32, device=device)

    k_res = torch.zeros((b, residual_block_size, h, d), device=device, dtype=dtype)
    v_res = torch.zeros((b, residual_block_size, h, d), device=device, dtype=dtype)
    k_res_cache, v_res_cache = cache.update_residual(k_new, v_new, layer_idx)
    cur_len = k_res_cache.shape[1]
    k_res[:, :cur_len], v_res[:, :cur_len] = k_res_cache, v_res_cache

    out_bit, k_pack_new, k_params_new, v_pack_new, v_params_new = fwd_kvcache_int(
        q, k_pack, k_params, v_pack, v_params,
        k_res, v_res, seqlens_k,
        k_pack_new, k_params_new, v_pack_new, v_params_new,
        None, sm_scale, quant_mode, group_size,
        residual_block_size, cur_len, num_bits)

    k_state = torch.cat([k_state, k_new], dim=1)
    v_state = torch.cat([v_state, v_new], dim=1)
    mae = (out_bit - attention_ref(q, k_state, v_state)).abs().mean().item()
    print(f"step {step}: seqlens_k={seqlens_k[0].item()} new_lens={cur_len} MAE={mae:.5f}")
```

**操作步骤**：保存为 `u2l3_minimal.py`，在编译好扩展的环境执行 `python u2l3_minimal.py`；随后把 `num_bits` 与 `group_size` 同步改为 `(2, 32)`（`pack_nums` 自动变为 8）再跑一次。

**需要观察的现象**：每步打印的 `seqlens_k + new_lens` 恒等于 1024 + step + 1；MAE 非零但量级很小（4-bit、group_size=32 下参考 `test.py` 的输出通常在 1e-3 量级）；5 轮内不会触发 `update_pack` 拼回。

**预期结果**：2-bit 的 MAE 明显大于 4-bit（位宽减半、每组共享 scale 的样本更多）。具体数值**待本地验证**——这正是你建立「量化代价」直觉的第一手数据，建议记录下来与 u7-l1 的分层测试对照。

## 6. 本讲小结

- `bit_decode` 包只暴露两个功能 API：`kvcache_pack_int`（prefill 量化打包）与 `fwd_kvcache_int`（decode 低比特注意力），外加三个 cache 类；用户代码永远不必直接 import `bit_decode_cuda`。
- `kvcache_pack_int` 无返回值：`k_pack/k_params/v_pack/v_params` 是调用方预分配的 out 参数；输入先 reshape 成 `(b*s, h, d)` 的 varlen 视图，序列边界由 `cu_seqlens_k` 承载。
- `fwd_kvcache_int` 返回 5 元组：`out_bit` 是注意力输出；`k_pack_new` 等 4 个是「残余攒满一块」时 kernel 顺带产出的新量化块，仅在 `new_lens == residual_block_size` 那一轮有效，由调用方判据消费。
- 长度语义要分清：`opt_seqlens_k` = 打包主缓存的 token 数（每 batch 一个的张量），`new_lens` = 残余区有效长度（标量），两者之和才是总 KV 长度。
- `num_bits` 在 Python 层 if/elif 分流到 `kvcache_pack_int2/4` 与 `fwd_kvcache_int2/4` 四个绑定函数，在 C++ 侧成为模板参数（编译期常量）；Python 层还写死了 6 个尾部开关（is_causal=False、无窗口、无 softcap 等）。
- 当前仓库实际启用的组合是 `k-channel` + `group_size ∈ {32, 128}`：签名默认值 `"k-tensor"` 只是 FlashAttention 的历史遗留，传非法组合会**静默无操作**而非报错。

## 7. 下一步学习建议

- **下一讲（u3-l1）** 将跨过本讲的终点 `bit_decode_cuda`，进入 `decode_api.cpp` 内部：`mha_fwd_kvcache` 模板函数的形状校验宏、GQA 重排与强制 split 路径——本讲 4.3.3 的两处「预热」正是为它准备的。
- 建议顺带阅读 [csrc/bit_decode/decode_api.cpp:240-280](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L240-L280) 的 `num_splits_heuristic`，提前感受「decode 时 batch 太小、必须切 KV 维」的动机。
- 若你想先看模型层如何使用这两个接口，可跳读 `evaluation/llama.py` 中 `LlamaBitDecoding.forward` 的 decode 分支（u6-l2 的主题），检验本讲的参数表是否已足以让你读懂那段调用。
