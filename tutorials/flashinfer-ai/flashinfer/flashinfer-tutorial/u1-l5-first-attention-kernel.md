# 第一个注意力算子：single_decode / single_prefill 实践

## 1. 本讲目标

本讲是你在 FlashInfer 中**真正跑通第一个 GPU kernel** 的一讲。读完本讲，你应该能够：

1. 用最简 API `single_decode_with_kv_cache` 和 `single_prefill_with_kv_cache` 跑一次注意力计算，拿到正确的输出张量。
2. 亲眼观察到「首次调用触发 JIT 编译、第二次调用几乎零开销」这一贯穿全项目的现象，并能用「两级缓存」解释它。
3. 清楚这两个函数对输入张量的 `dtype`、`device`、`shape` 三项约定，避免最常见的初学者报错。

本讲**只读高层 Python API**，不深入 `csrc/` 与 `include/` 的 kernel 实现——那是后续单元的事。我们把注意力放在「怎么用」和「第一次调用到底发生了什么」上。

## 2. 前置知识

在动手之前，用最朴素的语言把几个概念交代清楚。

**注意力（Attention）是什么。** 给定一个查询向量序列 \(Q\) 和一组键值对 \(K, V\)，注意力计算的是：

\[
\mathrm{Attention}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d}}\right)V
\]

其中 \(d\) 是每个头的维度（`head_dim`）。直观地说：用 \(Q\) 去和每一个 \(K\) 算相似度，归一化成权重，再用权重对 \(V\) 做加权求和。行内写作 \(QK^{\top}\)，分母里的 \(\sqrt{d}\) 就是源码里的 `sm_scale`（默认取 \(1/\sqrt{d}\)）。

**decode 与 prefill 的区别。** 这是 LLM 推理服务里最重要的两阶段：

- **prefill（预填充）**：处理一整段新的输入 prompt，query 长度 `qo_len` 通常远大于 1，需要计算 query 序列里**每一个位置**对 KV 的注意力。对应函数 `single_prefill_with_kv_cache`。
- **decode（解码）**：每生成一个新 token，query 只有**一个位置**（`qo_len=1`），但要从已经积累的 KV cache 里读取历史。对应函数 `single_decode_with_kv_cache`。

理解这个区别就够了：**prefill 的 q 是一段序列，decode 的 q 是单个向量**。这一点直接决定了两个函数输入张量的形状约定（见下文）。

**KV Cache。** decode 阶段不必每次都重算历史 token 的 K、V，而是把历史 K、V 缓存起来。本讲用的是「单请求、连续存储」的最简形式（还不是分页 Paged KV Cache，那是第 3 单元的内容）。

**torch.Tensor 的 device / dtype / shape。** FlashInfer 是 GPU kernel 库，所有输入张量必须在 CUDA 设备上（`device="cuda"`）；dtype 一般用半精度（`float16`/`bfloat16`）；shape 必须严格匹配函数约定。这三项里任何一项不对，都会直接报错。

**JIT 编译（承接 u1-l2 / u1-l4）。** FlashInfer 默认「用到才编译」：第一次调用某个（参数组合确定的）kernel 时，会现场用 ninja + nvcc 编译出 `.so` 并加载；之后再遇到完全相同的参数组合，就直接复用。本讲正是要**亲历**这个过程。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/README.md) | 项目首页的 Basic Usage 示例，本讲的起点 |
| [flashinfer/decode.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py) | `single_decode_with_kv_cache` 的实现 |
| [flashinfer/prefill.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py) | `single_prefill_with_kv_cache` 的实现 |
| [flashinfer/jit/attention/modules.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py) | `get_single_decode_uri` / `gen_single_decode_module` 等 JIT 生成器 |
| [flashinfer/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py) | `SINGLE_KERNEL_TMP_SIZE`、`MaskMode`、`determine_attention_backend` 等公共工具 |
| [examples/pytorch/flashinfer_modules.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/examples/pytorch/flashinfer_modules.py) | 官方 PyTorch 示例，展示这些 API 如何在模型里被组装 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 single decode**、**4.2 single prefill**、**4.3 首次调用的 JIT 编译与两级缓存（基础用法）**。

### 4.1 single_decode：单请求解码注意力

#### 4.1.1 概念说明

`single_decode_with_kv_cache` 处理「**一个请求、生成一个 token**」的最简单 decode 场景。它的输入是一个查询向量 \(q\)（代表当前要生成的那一个位置）和已经存好的 \(K, V\) 缓存，输出是这一个位置经过注意力后的向量。

它的「简单」体现在三方面：

1. **没有批处理**：只有一个请求，不需要 batch 维度。
2. **没有分页**：KV 是一段连续的张量，不是 Paged KV Cache。
3. **没有 plan/run 两段式**：一次性调用就出结果（分页版本才有 `plan` 步骤，见 u3-l3）。

正因为简单，它是理解 FlashInfer 调用约定的**最佳入门**。

#### 4.1.2 核心流程

调用 `single_decode_with_kv_cache(q, k, v)` 时，内部大致做这几件事：

```
1. 参数校验：检查 pos_encoding_mode、kv_layout 是否合法
2. 分配临时 workspace tmp（固定大小，uint8 字节缓冲）
3. 推导默认值：sm_scale = 1/sqrt(head_dim)、rope_scale=1.0、rope_theta=1e4 等
4. 取/编译 JIT 模块：get_single_decode_module(dtype, head_dim, ...)  ← 首次会触发编译
5. 调用 module.run(q, k, v, tmp, out, ...) 在 GPU 上执行
6. （可选）应用 v_scale，返回 out（和可选的 lse）
```

注意：`use_tensor_cores` 默认为 `False`，此时走的是专门的 decode kernel 路径；若设为 `True`，内部会转而复用 prefill kernel（对 GQA 大 group size 更快）。初学保持默认即可。

#### 4.1.3 源码精读

函数签名与文档串起来看。先看它在 [flashinfer/decode.py:456-473](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L456-L473) 的定义——注意上面挂着 `@flashinfer_api(trace=...)` 装饰器（用于日志与 trace，本讲先忽略）：

这段签名说明：`q/k/v` 是必填，其余如 `kv_layout`、`pos_encoding_mode`、`sm_scale` 等都有默认值。函数对输入形状的约定写在 docstring 里，[flashinfer/decode.py:478-487](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L478-L487)：

- `q`：`[num_qo_heads, head_dim]`
- `k`、`v`（NHD 布局）：`[kv_len, num_kv_heads, head_dim]`
- `k`、`v`（HND 布局）：`[num_kv_heads, kv_len, head_dim]`

默认布局是 `"NHD"`，即「先序列长度、再头数、最后维度」。注意约束：**`num_qo_heads` 必须是 `num_kv_heads` 的整数倍**；不等时自动使用分组查询注意力（GQA）。

再看实际执行体。函数开头做参数校验和默认值推导，[flashinfer/decode.py:552-567](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L552-L567) 做的事：校验 `pos_encoding_mode` / `kv_layout`、分配 `tmp`、把 `None` 的 `sm_scale`/`rope_scale`/`rope_theta` 填上默认值。

然后是关键的「取模块 + 执行」部分。当 `use_tensor_cores=False`（默认）时走 else 分支，[flashinfer/decode.py:612-639](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L612-L639)：

```python
out = torch.empty_like(q)
get_single_decode_module(
    q.dtype, k.dtype, q.dtype,
    head_dim,        # head_dim_qk
    head_dim,        # head_dim_vo
    PosEncodingMode[pos_encoding_mode].value,
    window_left != -1,
    logits_soft_cap > 0,
).run(q, k, v, tmp, out, lse, ..., rope_scale, rope_theta)
```

`get_single_decode_module(...)` 返回一个已编译的模块对象，`.run(...)` 才是真正发起 GPU kernel。`get_single_decode_module` 本身是 `@functools.cache` 装饰的，[flashinfer/decode.py:91-94](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L91-L94)：

```python
@functools.cache
def get_single_decode_module(*args):
    uri = get_single_decode_uri(*args)
    module = gen_single_decode_module(*args).build_and_load()
    ...
```

这就是两级缓存的「第一级」（内存级）——同一个参数组合第二次进来，直接返回上次加载好的模块，根本不会再走 `build_and_load()`。第二级（磁盘级）我们在 4.3 节展开。

docstring 里还自带一个最小可运行示例，[flashinfer/decode.py:533-544](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L533-L544)，几乎和 README 的一致。

#### 4.1.4 代码实践

**实践目标**：用 `single_decode_with_kv_cache` 跑一次 decode，并体会首次编译耗时。

**操作步骤**（示例代码，请在你已按 u1-l2 安装好 flashinfer 的环境里运行）：

```python
# 示例代码
import time
import torch
import flashinfer

kv_len = 4096
num_qo_heads = 32
num_kv_heads = 32          # MHA：q/kv 头数相等
head_dim = 128

q = torch.randn(num_qo_heads, head_dim, device="cuda:0", dtype=torch.float16)
k = torch.randn(kv_len, num_kv_heads, head_dim, device="cuda:0", dtype=torch.float16)
v = torch.randn(kv_len, num_kv_heads, head_dim, device="cuda:0", dtype=torch.float16)

# 第一次调用：触发 JIT 编译
torch.cuda.synchronize()
t0 = time.time()
o = flashinfer.single_decode_with_kv_cache(q, k, v)
torch.cuda.synchronize()
print("首次调用(含编译)耗时:", time.time() - t0)
print("输出形状:", o.shape)   # 期望: torch.Size([32, 128])

# 第二次调用：命中缓存，几乎零开销
torch.cuda.synchronize()
t1 = time.time()
o2 = flashinfer.single_decode_with_kv_cache(q, k, v)
torch.cuda.synchronize()
print("第二次调用耗时:", time.time() - t1)
```

**需要观察的现象**：

1. 首次调用会有明显的「卡顿」（几秒到几十秒不等，取决于机器），同时终端（若设了 `FLASHINFER_JIT_VERBOSE=1`）会打印 ninja 编译日志。
2. 输出形状为 `[32, 128]`，与 `q` 形状一致。
3. 第二次调用耗时相比首次会**骤降几个数量级**。

**预期结果**：输出 `torch.Size([32, 128])`；首次耗时远大于第二次。具体编译耗时与机器/架构强相关，**待本地验证**（请记录你机器上的两个数字）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `num_kv_heads` 改成 `8`（GQA，`num_qo_heads=32`），输入张量形状该怎么改？还会触发一次新的编译吗？

> **参考答案**：`q` 仍是 `[32, 128]`；`k`/`v` 改为 `[kv_len, 8, 128]`（NHD）。因为 `num_qo_heads` 是 `num_kv_heads` 的整数倍（32 是 8 的倍数），合法。**不会**触发新编译——JIT 的缓存键只含 dtype/head_dim/pos_encoding_mode 等编译期参数（见 4.3 的 URI），头数比例是运行期参数，不影响编译产物。

**练习 2**：把 `q` 的 dtype 改成 `bfloat16`（k/v 也改成 bfloat16），调用还会复用 `float16` 那次的编译结果吗？

> **参考答案**：不会。dtype 是 URI 的一部分，不同 dtype 会得到不同的模块名，因此会**再编译一次**。这正体现了「按参数组合特化」的 JIT 思想。

### 4.2 single_prefill：单请求预填充注意力

#### 4.2.1 概念说明

`single_prefill_with_kv_cache` 处理「**一个请求、一整段 query 序列**」的场景：给定长度为 `qo_len` 的 query 序列，计算它对 KV 的注意力，输出**同样长度 `qo_len`** 的结果序列。它是 prefill 阶段、也是朴素训练式注意力的最简封装。

与 decode 的关键差别只有一点：**query 多了一个序列长度维度 `qo_len`**。其余（KV 布局、head_dim 约定）与 decode 一致。另外它多了一个 `causal`（因果掩码）开关——语言模型预填充时通常 `causal=True`，保证每个位置只能看到自己和之前的位置。

#### 4.2.2 核心流程

```
1. 参数校验 + 默认值推导（与 decode 类似）
2. 处理 custom_mask：若给了 custom_mask 但没给 packed_custom_mask，则 packbits 打包
3. 确定 mask_mode：CUSTOM / CAUSAL / NON_CAUSAL
4. backend == "auto" 时，调用 determine_attention_backend 按 设备/dtype/head_dim 选 fa2 或 fa3
5. 取/编译 JIT 模块 get_single_prefill_module(...)
6. 调用 module.run(q, k, v, tmp, out, lse, mask_mode, ...) 执行
7. 返回 out（和可选 lse）
```

注意 prefill 有 **backend 选择**：`"auto"`/`"fa2"`/`"fa3"`。`fa3`（FlashAttention-3）需要 Hopper（SM90a）及以上。`auto` 会自动判断（详见 u3-l5）。

#### 4.2.3 源码精读

函数定义在 [flashinfer/prefill.py:1155-1180](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1155-L1180)。关键默认值：`causal=False`、`kv_layout="NHD"`、`pos_encoding_mode="NONE"`、`backend="auto"`、`return_lse=False`。

输入形状约定见 [flashinfer/prefill.py:1186-1195](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1186-L1195)：

- `q`：`[qo_len, num_qo_heads, head_dim_qk]`
- `k`（NHD）：`[kv_len, num_kv_heads, head_dim_qk]`
- `v`（NHD）：`[kv_len, num_kv_heads, head_dim_vo]`

注意 prefill 允许 `head_dim_qk` 与 `head_dim_vo` 不同（部分模型输出维度与 query 维度不同），而 decode 里二者通常相等。

掩码模式的判定逻辑在 [flashinfer/prefill.py:1324-1336](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1324-L1336)：优先级是 `packed_custom_mask` > `custom_mask` > `causal`。三种模式对应 `MaskMode` 枚举，定义在 [flashinfer/utils.py:38-41](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L38-L41)：`NON_CAUSAL=0`、`CAUSAL=1`、`CUSTOM=2`。

后端自动选择在 [flashinfer/prefill.py:1359-1369](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1359-L1369)，当 `backend=="auto"` 时调用 `determine_attention_backend`（实现于 [flashinfer/utils.py:483](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L483)），根据设备、dtype、head_dim 等返回 `"fa2"` 或 `"fa3"`。这也是为什么同一个 API 在不同 GPU 上行为可能不同。

docstring 自带示例在 [flashinfer/prefill.py:1277-1305](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1277-L1305)，演示了 `causal=True` 与 `custom_mask` 两种用法，并断言两者结果近似相等——这是理解「因果掩码等价于一个下三角 custom mask」的好例子。

#### 4.2.4 代码实践

**实践目标**：用 `single_prefill_with_kv_cache` 跑一次因果 prefill，并验证 `causal=True` 与等价的下三角 custom mask 结果一致。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch
import flashinfer

qo_len, kv_len = 128, 4096
num_qo_heads, num_kv_heads, head_dim = 32, 4, 128   # GQA

q = torch.randn(qo_len, num_qo_heads, head_dim, device="cuda:0", dtype=torch.float16)
k = torch.randn(kv_len, num_kv_heads, head_dim, device="cuda:0", dtype=torch.float16)
v = torch.randn(kv_len, num_kv_heads, head_dim, device="cuda:0", dtype=torch.float16)

# 因果 prefill
o = flashinfer.single_prefill_with_kv_cache(q, k, v, causal=True)
print("prefill 输出形状:", o.shape)   # 期望: torch.Size([128, 32, 128])
```

**需要观察的现象**：输出形状为 `[qo_len, num_qo_heads, head_dim] = [128, 32, 128]`；同样首次调用会触发一次编译。

**预期结果**：形状正确，数值为有限值（无 NaN/Inf）。是否走 fa3 取决于你的 GPU 是否为 SM90a+，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `single_prefill_with_kv_cache` 需要 `causal` 参数，而 `single_decode_with_kv_cache` 没有？

> **参考答案**：decode 的 query 只有一个位置，且是序列最末端，天然能看到所有历史 KV，不存在「未来信息泄露」问题，因此不需要掩码。prefill 的 query 是一整段序列，必须用因果掩码防止位置 \(i\) 看到位置 \(i+1\) 之后的 KV，否则就变成「双向」注意力了。

**练习 2**：`backend="auto"` 在一张 Hopper 卡（SM90a）上、`head_dim=128`、fp16 输入时，通常会选哪个后端？

> **参考答案**：通常选 `"fa3"`（FlashAttention-3），因为它在 Hopper 上更快。具体由 `determine_attention_backend` 综合判断（dtype、head_dim、是否 fp16 qk reduction、是否 custom mask 等），**待本地验证**你机器上的实际选择（可在调用后查看日志或源码逻辑）。

### 4.3 首次调用的 JIT 编译与两级缓存（基础用法）

#### 4.3.1 概念说明

前面两个模块都提到「首次调用会编译」。本模块把这个现象讲透，因为它是理解整个 FlashInfer 的钥匙。

FlashInfer 不预编译所有 kernel，而是**用到哪个参数组合才编译哪个**。同一个 `single_decode_with_kv_cache`，换成不同 dtype 或 head_dim，就是**不同的 kernel**，需要分别编译。为了避免重复编译，FlashInfer 设计了**两级缓存**：

| 级别 | 位置 | 缓存键 | 失效条件 |
|------|------|--------|----------|
| 第一级（内存） | Python 进程内 | JIT 模块函数的全部参数元组 | 进程退出即失效 |
| 第二级（磁盘） | `~/.cache/flashinfer/` 下的 `.so` | URI（参数 + 源码哈希 + arch） | 源码改动 / 换架构 / 清缓存 |

#### 4.3.2 核心流程

一次 `single_decode_with_kv_cache(q, k, v)`（首次）的完整时间线：

```
用户调用
  └─ get_single_decode_module(dtype_q=fp16, dtype_kv=fp16, dtype_o=fp16,
                              head_dim_qk=128, head_dim_vo=128,
                              posenc=0, use_swa=False, use_logits_cap=False)
       │
       │ ① @functools.cache 查内存：参数元组没见过 → miss
       │
       └─ gen_single_decode_module(...).build_and_load()
            │
            │ ② 算 URI（参数拼成字符串）
            │
            │ ③ 查磁盘：~/.cache/flashinfer/.../<uri>/ 下有没有 .so
            │    - 有 → 直接 dlopen 加载（第二级命中）
            │    - 无 → 用 ninja + nvcc 现场编译（首次编译，慢）
            │
            └─ 返回已加载的 module 对象，存进 @functools.cache
  └─ module.run(...) 在 GPU 执行
```

第二次用**相同参数**调用：

```
get_single_decode_module(同样的 8 个参数)
  └─ @functools.cache 查内存：命中 → 直接返回 module 对象（第一级命中）
       └─ module.run(...) 立即执行，无需任何编译/加载
```

如果**重启了 Python 进程**但没清磁盘缓存：第一级（内存）失效，但第二级（磁盘 `.so`）仍在，所以 `build_and_load()` 会直接加载已编译的 `.so`，比首次快很多但仍有加载开销。

#### 4.3.3 源码精读

**第一级缓存**：`get_single_decode_module` 上的 `@functools.cache`，见 [flashinfer/decode.py:91-94](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L91-L94)。`functools.cache` 以「全部位置参数组成的元组」为键——这里就是那 8 个编译期参数（两个 dtype 实际是 `q.dtype, k.dtype, q.dtype` 共三个，加上 head_dim_qk、head_dim_vo、posenc、use_swa、use_logits_cap）。

**第二级缓存的键 = URI**。URI 由 `get_single_decode_uri` 拼接，[flashinfer/jit/attention/modules.py:45-64](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L45-L64)：

```python
return (
    f"single_decode_with_kv_cache_dtype_q_{...}_"
    f"dtype_kv_{...}_dtype_o_{...}_"
    f"head_dim_qk_{head_dim_qk}_head_dim_vo_{head_dim_vo}_"
    f"posenc_{pos_encoding_mode}_"
    f"use_swa_{use_sliding_window}_use_logits_cap_{use_logits_soft_cap}"
)
```

可以看到 URI 把所有「会影响生成的代码」的参数都编进去了。dtype 一变、head_dim 一变，URI 就不同，于是编译成另一个 `.so`。注意：`num_qo_heads`、`num_kv_heads`、`kv_len` 这些**运行期形状**不在 URI 里——它们是 kernel 启动参数，不需要为每个形状单独编译。

**模块生成**：`gen_single_decode_module` 在 [flashinfer/jit/attention/modules.py:446-487](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L446-L487)，先算 URI，再调用 `gen_customize_single_decode_module` 渲染变体（`variant_name` 是一段 C++ 模板参数，决定 kernel 行为），最后返回 `JitSpec`。`.build_and_load()`（在 `flashinfer/jit/core.py`，详见 u2-l2）负责落盘编译与加载。

**临时 workspace**：两个函数都分配了一个固定大小的字节缓冲 `tmp`，大小由 `SINGLE_KERNEL_TMP_SIZE` 定义，[flashinfer/utils.py:207](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L207)：

```python
SINGLE_KERNEL_TMP_SIZE = 32 * 1024 * 1024   # 32 MiB
```

这是 kernel 运行时的 scratch space（比如存放 softmax 中间统计），与「单请求」场景匹配；批处理版本（u3-l3）会用更大的、由 `plan` 动态决定的 workspace。

#### 4.3.4 代码实践

**实践目标**：亲手验证两级缓存，并理解 URI 如何区分不同 kernel。

**操作步骤**（示例代码）：

```python
# 示例代码
import time
import torch
import flashinfer

def bench(fn):
    torch.cuda.synchronize(); t = time.time(); fn(); torch.cuda.synchronize()
    return time.time() - t

q = torch.randn(32, 128, device="cuda:0", dtype=torch.float16)
k = torch.randn(2048, 32, 128, device="cuda:0", dtype=torch.float16)
v = torch.randn(2048, 32, 128, device="cuda:0", dtype=torch.float16)

# (A) 首次：fp16, head_dim=128
print("A 首次(编译):", bench(lambda: flashinfer.single_decode_with_kv_cache(q, k, v)))
# (B) 第二次：同参数 → 内存命中
print("B 第二次(内存命中):", bench(lambda: flashinfer.single_decode_with_kv_cache(q, k, v)))

# (C) 换 head_dim=64 → 不同 URI → 重新编译
q2 = torch.randn(32, 64, device="cuda:0", dtype=torch.float16)
k2 = torch.randn(2048, 32, 64, device="cuda:0", dtype=torch.float16)
v2 = torch.randn(2048, 32, 64, device="cuda:0", dtype=torch.float16)
print("C 换 head_dim(重新编译):", bench(lambda: flashinfer.single_decode_with_kv_cache(q2, k2, v2)))

# (D) 换 dtype=bfloat16 → 不同 URI → 重新编译
qb = q.to(torch.bfloat16); kb = k.to(torch.bfloat16); vb = v.to(torch.bfloat16)
print("D 换 dtype(重新编译):", bench(lambda: flashinfer.single_decode_with_kv_cache(qb, kb, vb)))
```

**需要观察的现象**：

- A（首次）远慢于 B。
- B 极快（内存命中，无编译无加载）。
- C、D 都会比 B 慢——因为 URI 变了，触发新一轮 `build_and_load()`。
- 如果你之前已经跑过相同参数组合（`.so` 在磁盘缓存里），C/D 可能比 A 快（第二级磁盘命中）。

**进一步探索磁盘缓存**：在另一个终端运行 `ls ~/.cache/flashinfer/`（或 `flashinfer show-config` 显示的 JIT 目录），你会看到与 URI 同名的目录，里面是 `build.ninja` 和编译出的 `.so`。把某个 `.so` 删掉再跑（A），会再次触发编译。

**预期结果**：A > B，C、D 接近 A（首次编译）或介于 A、B 之间（磁盘命中）。**待本地验证**你机器上的具体数字，并打开 `FLASHINFER_JIT_VERBOSE=1` 观察编译日志。

#### 4.3.5 小练习与答案

**练习 1**：下面哪些改动会触发 `single_decode_with_kv_cache` 的**重新编译**？(a) `kv_len` 从 2048 改成 4096；(b) dtype 从 fp16 改成 bf16；(c) `num_qo_heads` 从 32 改成 64；(d) `head_dim` 从 128 改成 64；(e) 重启 Python 进程。

> **参考答案**：会重新编译的是 (b) 和 (d)——它们改变了 URI。(a)、(c) 是运行期形状/头数，不在 URI 里，不触发编译（但 (c) 头数变化仍合法）。(e) 重启进程会让**第一级内存缓存**失效，但**第二级磁盘 `.so`** 仍在，所以通常只需 dlopen 加载而非重新 nvcc 编译；只有在磁盘缓存被清或源码被改时才会真正重新编译。

**练习 2**：为什么 FlashInfer 选择「把 head_dim 编进 URI、但不把 kv_len 编进 URI」？

> **参考答案**：head_dim 是 kernel 模板里的编译期常量（影响循环展开、寄存器分配、共享内存布局），为每个 head_dim 生成特化代码能显著提速；而 kv_len 是运行期变量，kernel 通过循环边界动态处理，没必要也无法为每个可能的长度都编译一份（否则编译产物爆炸）。这是「编译期特化 vs 运行期参数」的经典权衡。

## 5. 综合实践

把本讲三个模块串起来：**用 decode + prefill 模拟一次「先读 prompt、再生成一个 token」的极简推理**。

1. 构造一段 prompt，作为 `single_prefill_with_kv_cache` 的输入（`causal=True`），拿到最后一个位置的输出。
2. 把整段 prompt 的 K、V 作为 `single_decode_with_kv_cache` 的 KV cache，把「最后一个位置的输出」（经过一次你自己的线性投影即可，或直接复用 prompt 末位的 q）作为新的 query，调用 decode，得到下一个 token 的注意力结果。
3. 用 `time` 测量：prefill 的首次编译耗时、decode 的首次编译耗时，以及各自的第二次调用耗时。
4. 写一段话回答：为什么第二次调用都快？为什么 prefill 和 decode 是**两个不同的 `.so`**（提示：URI 不同）？

> 这一步重在把「prefill 出序列、decode 出单点」「不同 kernel 各自 JIT 各自缓存」串成一条完整认知。数值正确性可放宽（你未必要真实模型权重），重点是观察编译与缓存行为。完整推理流水线（含分页 KV、批处理）在 u3 单元展开。

## 6. 本讲小结

- `single_decode_with_kv_cache` 处理「单请求、单 query 向量」的解码，`q` 形状 `[num_qo_heads, head_dim]`，KV 默认 NHD 布局 `[kv_len, num_kv_heads, head_dim]`。
- `single_prefill_with_kv_cache` 处理「单请求、一段 query 序列」的预填充，`q` 形状 `[qo_len, num_qo_heads, head_dim]`，多一个 `causal` 因果掩码开关和 `backend`（fa2/fa3）选择。
- 两个函数都要求 `num_qo_heads` 是 `num_kv_heads` 的整数倍（支持 GQA），且张量必须在 CUDA 设备上。
- 首次调用触发 JIT 编译（ninja + nvcc），耗时显著；之后命中**第一级内存缓存**（`@functools.cache`）则几乎零开销。
- 第二级是磁盘 `.so` 缓存，键为 URI——URI 编码了 dtype、head_dim、posenc 等**编译期**参数，但不包含 `kv_len`、头数等**运行期**形状。
- 改 dtype 或 head_dim 会产生新 URI、触发重新编译；改形状或头数则不会。

## 7. 下一步学习建议

本讲只用了「单请求、连续 KV」的最简形式。真正的推理服务要处理**动态批处理**与**分页 KV Cache**，这正是第 3 单元的主题：

- **u3-l1** 会讲清 decode/prefill/append 三阶段、为什么需要 Paged/Ragged KV、以及 `plan/run` 两段式 API 的动机。
- **u3-l3** 会深入 `BatchDecodeWithPagedKVCacheWrapper` 的 `plan/run` 全流程，那是本讲 `single_decode` 的「批处理 + 分页」升级版。
- 若你对「JIT 编译到底怎么生成代码」更感兴趣，可以先跳到 **u2 单元**（JIT 编译系统），再回来学 u3。

建议接下来**按 u3-l1 → u3-l2 → u3-l3** 的顺序读，把「单请求」升级到「批处理 + 分页」。
