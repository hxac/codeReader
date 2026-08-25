# 跑通第一个例子：GSM8K 长上下文生成与 kernel 正确性测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 在有 GPU 的机器上运行 `evaluation/test.py`，看懂它打印的每一行输出（`residual_len`、`cur_residual_len`、每轮的平均绝对误差）。
2. 在有 GPU 的机器上运行 `evaluation/scripts/example.sh`，让 Qwen3-8B 或 Llama-3.1-8B 用 `bit_decoding` 后端在 GSM8K 长提示上生成文本，并知道如何通过命令行参数切换 `num_bits` / `quant_mode` / `group_size` / `attn_backend`。
3. 读懂 `test.py` 中「prefill 打包 → 逐轮 decode」的完整调用序列：`kvcache_pack_int` 与 `fwd_kvcache_int` 如何配合 `DynamicCache` 的 `update_pack` / `update_residual` / `clear_residual` 工作。
4. 能解读每轮打印的平均绝对误差（MAE）数值代表的精度量级，并理解 2-bit 与 4-bit 的误差差异从何而来。
5. 没有 GPU 时，也能通过通读 `test.py` 写出每一步张量形状的变化表（本讲给出无 GPU 替代实践）。

## 2. 前置知识

本讲假设你已完成 u1-l2（编译安装出 `bit_decode_cuda` 扩展）和 u1-l3（知道仓库有三个目录：`bit_decode/`、`csrc/bit_decode/`、`evaluation/`）。在此基础上，补充三个通俗概念：

- **prefill 与 decode**：LLM 生成文本分两个阶段。prefill 阶段一次性处理整个提示词（几百到几千个 token），产出第一份 KV cache；decode 阶段每步只处理 1 个新 token，同时要读全部历史 KV cache。BitDecoding 加速的正是 decode 阶段——因为此时瓶颈是读 KV cache 的显存带宽。
- **参考实现（reference）与平均绝对误差（MAE）**：验证一个加速算法是否正确，标准做法是写一个「慢但肯定对」的朴素版本（这里是用 PyTorch einsum 直接算注意力），然后比较两者输出的差异。MAE 的定义是：

  \[
  \mathrm{MAE} = \frac{1}{N}\sum_{j=1}^{N} \left| out_{bit,j} - out_{ref,j} \right|
  \]

  MAE 越小说明低比特量化引入的误差越小。它不为零是**正常的**——量化本身就是有损压缩，我们关心的是误差量级是否小到不影响生成质量。
- **GSM8K**：一个小学数学应用题数据集（Grade School Math 8K），每条样本是一问一答。`example.py` 把 15 道题的问答拼成一段长提示（模拟长上下文），再让模型回答第 16 道题，最后检查答案是否仍然正确——这是端到端检验「量化后的 KV cache 有没有把模型算坏」。

另外回顾一个上一讲的关键结论：BitDecoding 的 KV cache 被拆成两部分——**低比特打包主缓存**（`k_pack`/`k_params`/`v_pack`/`v_params`）和 **FP16 残余缓存**（最新的、还没攒满一个块的 token）。本讲的代码实践会让你第一次亲眼看到这个机制运转。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py) | 不加载大模型的 kernel 正确性测试 | `attention_ref` 参考、prefill 打包、32 轮 decode 循环、误差打印 |
| [evaluation/example.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py) | 用真实 8B 模型在 GSM8K 上做端到端生成 | 命令行参数、config 注入、cache 猴子补丁 |
| [evaluation/scripts/example.sh](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/scripts/example.sh) | 一键运行 example.py 的脚本 | 默认参数组合 |
| [bit_decode/bit_decode_interface.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py) | Python 侧仅有的两个 API | `kvcache_pack_int` / `fwd_kvcache_int` 的参数表 |
| [bit_decode/models/cache_utils.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py) | 改造版 transformers 缓存类 | `update_residual` / `update_pack` / `clear_residual` 三个方法 |

## 4. 核心概念与源码讲解

### 4.1 attention_ref 参考实现：什么是「正确答案」

#### 4.1.1 概念说明

`attention_ref` 是 `test.py` 内置的朴素注意力实现：不量化、不打包、不融合，直接按数学定义用 `torch.einsum` 算。它的角色是**裁判**——每轮 decode 后，BitDecoding kernel 的输出都要和它比一次 MAE。理解这段 20 行的代码，你才能理解后面所有输出数字的含义。

单个 query token 对一整段 KV cache 的注意力数学定义是：

\[
\mathrm{out} = \frac{\displaystyle\sum_{i=1}^{S} \exp\!\left(\frac{q \cdot k_i}{\sqrt{d}}\right) v_i}{\displaystyle\sum_{i=1}^{S} \exp\!\left(\frac{q \cdot k_i}{\sqrt{d}}\right)}
\]

其中 \(S\) 是 KV cache 的 token 数，\(d\) 是 head_dim（本测试中为 128）。

#### 4.1.2 核心流程

```
输入 q (b, 1, h, d)、k/v (b, S, h_kv, d)
  1. scores = einsum("bthd,bshd->bhts", q/√d, k)   # 每个 query 对每个 key 的点积
  2. attention = softmax(scores, dim=-1)             # 对所有 key 归一化成权重
  3. output = einsum("bhts,bshd->bthd", attention, v) # 权重加权求和 value
返回 output 和 attention（权重矩阵）
```

#### 4.1.3 源码精读

[evaluation/test.py:13-37](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L13-L37) 定义了 `attention_ref`，docstring 写明了四个张量的形状约定（q 是 4 维 `(batch, seqlen_q, nheads, head_dim)`，注意这不是 `(b, h, s, d)` 的 HF 惯用布局）。

关键三行：

- [evaluation/test.py:31](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L31)：`scores = torch.einsum("bthd,bshd->bhts", q / math.sqrt(d), k)`——先把 q 除以 \(\sqrt{d}\)（缩放因子融进 q），再算点积，得到 `(b, h, seqlen_q, seqlen_k)` 的分数张量。
- [evaluation/test.py:33](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L33)：对最后一维（key 维）做 softmax，得到归一化注意力权重。
- [evaluation/test.py:35](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L35)：权重对 v 加权求和，还原成 `(b, seqlen_q, nheads, head_dim)` 的输出。

这个「先算全部分数、再整体 softmax」的朴素写法，正是 FlashAttention 系列要避免的（中间矩阵 `bhts` 太大）。但作为参考实现，正确性一目了然。

#### 4.1.4 代码实践

**实践目标**：确认你理解 einsum 布局，并能在 CPU 上跑通参考实现（无需 GPU）。

1. 复制 `attention_ref` 到一个独立脚本（示例代码，非项目原有）：

   ```python
   # 示例代码：CPU 上验证 attention_ref 的小输入行为
   import torch, math
   torch.manual_seed(0)
   q = torch.rand(1, 1, 2, 8)          # (b, seqlen_q, nheads, d)
   k = torch.randn(1, 5, 2, 8)         # (b, seqlen_k, nheads_kv, d)
   v = torch.randn(1, 5, 2, 8)
   out, attn = attention_ref(q, k, v)  # 粘贴 test.py 中的函数定义
   print(out.shape)   # 预期 torch.Size([1, 1, 2, 8])
   print(attn.sum(-1))  # 每个注意力权重行求和应恒等于 1
   ```

2. 操作步骤：在任意有 PyTorch 的环境运行；`attn.sum(-1)` 应打印全 1（浮点误差内）。
3. 需要观察的现象：softmax 归一化后每一行权重和为 1；输出形状与 q 相同。
4. 预期结果：`out.shape == (1, 1, 2, 8)`，`attn.sum(-1)` 全 1。本实践可在 CPU 完成；若你的环境没有 torch，结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`test.py` 中 `sm_scale = 1.0 / math.sqrt(d)`（[evaluation/test.py:58](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L58)）与 `attention_ref` 里的 `q / math.sqrt(d)` 是什么关系？会不会重复缩放？

**答案**：`attention_ref` 内部已经做了 `q / math.sqrt(d)`，它不接收 `sm_scale` 参数；`sm_scale` 是传给 `fwd_kvcache_int` 的（[evaluation/test.py:144](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L144)），两条路径各缩放一次，不会重复。

**练习 2**：为什么参考实现敢用 `torch.softmax` 一次性对几千个 key 做 softmax，而 FlashAttention 类 kernel 要用「在线 softmax」？

**答案**：参考实现不追求性能，中间矩阵 `(b, h, 1, seqlen_k)` 虽大但一次性分配即可；kernel 要把 K/V 分块搬进有限的共享内存，必须边算边维护运行最大值与归一化因子（online softmax / rescale），这是 u5-l2 的主题。

**练习 3**：如果把 `attention = torch.softmax(scores, dim=-1)` 的 `dim` 改成 `dim=-2`，输出会变成什么？

**答案**：softmax 会沿 query 维（本测试中 seqlen_q=1，退化为对单元素 softmax，结果恒为 1）归一化而不是 key 维，加权求和会变成「把所有 v 直接相加」，输出错误。`dim=-1` 才是对每个 query 在全部 key 上归一化。

### 4.2 test.py 的两阶段流程：prefill 打包 + 逐轮 decode

#### 4.2.1 概念说明

`test.py` 是 BitDecoding 的**最小可运行闭环**：不加载大模型，随机造一份 1024 token 的 KV cache，先打包（模拟 prefill），然后模拟 32 步 decode，每步都与 FP16 参考实现比误差。它把 Python 侧仅有的两个 API——`kvcache_pack_int`（打包）与 `fwd_kvcache_int`（解码注意力）——按真实模型的调用顺序串了起来，是后续所有单元的「锚点程序」。

#### 4.2.2 核心流程

```
配置：k-channel / num_bits=4 / group_size=32 / residual_block_size=128
      b=1, h=32, d=128, seqlen_q=1, seqlen_k=1024

【Round 1：prefill】
  1. residual_len = 1024 % 128 = 0 → 无初始残余，1024 个 token 全部进入打包流程
  2. 分配 4 个打包张量 k_pack/k_params/v_pack/v_params
  3. kvcache_pack_int(...) 把 FP16 K/V 量化打包写进这 4 个张量
  4. update_pack(...) 存入 DynamicCache

【Round 2~33：decode，循环 32 次】
  每轮：
  5. update_pack(None,...)  → 读出主缓存 4 个张量
  6. 造一个新 token 的 k_new/v_new
  7. update_residual(k_new, v_new) → 追加进 FP16 残余缓存，长度 +1
  8. 残余缓存拷进 128 行的补零缓冲区（k_residual/v_residual）
  9. fwd_kvcache_int(q, 主缓存, 残余, ...) → 返回 out + 4 个 *_new 张量
  10. 若残余攒满 128：update_pack(*_new) 拼回主缓存 + clear_residual
  11. 参考 FP16 KV 也追加新 token，attention_ref 算参考输出，打印 MAE
```

注意第 10 步：默认配置下 `seqlen_k=1024` 恰好整除 128，初始残余为空；32 轮里 `cur_residual_len` 从 1 涨到 32，**永远不会到 128**，所以默认的 32 轮测试根本不会触发拼回主缓存的分支——这是一个很容易被忽略的事实，也是本讲实践要验证的点之一。

#### 4.2.3 源码精读

**配置区**：[evaluation/test.py:40-58](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L40-L58) 设定 `quant_mode="k-channel"`、`num_bits=4`、`pack_nums = 16 / num_bits`（即一个 uint16 装 4 个 int4）、`group_size=32`、`residual_block_size=128`，以及形状参数 `b=1, nheads=32, nheads_k=32, d=128, seqlen_q=1, seqlen_k=1024`。

**prefill 数据准备**：[evaluation/test.py:64-70](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L64-L70) 随机生成 `q`（`torch.rand`，注意 q 是固定不变的 32 轮共用）和 `k_state`/`v_state`（`torch.randn`），然后计算 `residual_len = seqlen_k % residual_block_size`——1024 % 128 = 0，所以打印 `residual: False`。

**打包张量分配**：[evaluation/test.py:78-82](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L78-L82) 按布局分配 4 个张量。以默认配置（`pack_nums=4`，`group_size=32`，`seqlen_k_pack=1024`）为例：

| 张量 | 形状 | dtype | 含义 |
|---|---|---|---|
| `k_pack` | `(1, 1024/4=256, 32, 128)` | uint16 | K 的 int4 打包容器，seq 维被压缩 4 倍 |
| `k_params` | `(1, 1024/32=32, 32, 128)` | float32 | K 的 scale/zero，每 32 个 token 一组（k-channel：分组沿 seq，通道独立） |
| `v_pack` | `(1, 1024, 32, 128/4=32)` | uint16 | V 的 int4 打包，注意是 **d 维**被压缩 |
| `v_params` | `(1, 128/32=4, 32, 1024)` | float32 | V 的 scale/zero，分组沿 **head_dim**（v-tensor 布局，seq 在最后一维） |

这个「K 沿 seq 分组、V 沿 d 分组」的非对称布局是 u2-l1 的主题，这里先记住形状即可。

**打包调用**：[evaluation/test.py:97-106](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L97-L106) 调用 `kvcache_pack_int(k_state_past, k_pack, k_params, v_state_past, v_pack, v_params, None, cu_seqlens_k, seqlen_k_pack, quant_mode, group_size, num_bits)`。它在 [bit_decode/bit_decode_interface.py:12-45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L12-L45) 中把 `(b, seqlen, h, d)` reshape 成 `(b*seqlen, h, d)` 后，按 `num_bits` 分发到 `bit_decode_cuda.kvcache_pack_int4/int2`（[bit_decode/bit_decode_interface.py:26-43](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L26-L43)）。随后 [evaluation/test.py:107](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L107) 用 `update_pack` 存进缓存；[evaluation/test.py:110-113](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L110-L113) 另外预分配了 4 个 `*_new` 空缓冲（形状对应一个 `residual_block_size=128` 的块），decode 时传给 kernel 接住「攒满后新量化的块」。

**decode 循环**：[evaluation/test.py:116-160](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L116-L160)，每轮依次：

- [evaluation/test.py:121](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L121)：`update_pack(None, None, None, None, layer_idx)`——全传 `None` 是「只读」用法（见 4.3.3），拿回主缓存当前的 4 个张量。
- [evaluation/test.py:127-135](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L127-L135)：分配 128 行补零的 `k_residual`/`v_residual` 缓冲，把 `update_residual(k_new, v_new)` 追加后的残余缓存拷到缓冲区头部。补零是为了形状对齐 kernel 的固定块大小，真正的有效长度由 `cur_residual_len`（即 `new_lens` 参数，[evaluation/test.py:148](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L148)）告知 kernel，零填充部分不参与计算。
- [evaluation/test.py:137-150](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L137-L150)：调用 `fwd_kvcache_int(q, k_pack, k_params, v_pack, v_params, k_residual, v_residual, seqlens_k, k_pack_new, ..., sm_scale, quant_mode, group_size, residual_block_size, cur_residual_len, num_bits)`。其签名与 int4/int2 分发见 [bit_decode/bit_decode_interface.py:47-107](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L47-L107)，返回 `(out, k_pack_new, k_params_new, v_pack_new, v_params_new)` 五元组。
- [evaluation/test.py:152-154](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L152-L154)：仅当 `cur_residual_len == residual_block_size` 时，把 kernel 写好的 `*_new` 张量 `update_pack` 拼回主缓存并 `clear_residual` 清空残余区。
- [evaluation/test.py:156-160](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L156-L160)：FP16 参考 KV 也追加新 token，`attention_ref` 算参考输出，打印 `Round N: bitdecode vs pytorch: <MAE>`。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲眼看到 32 轮 decode 的误差曲线，并对比 4-bit 与 2-bit 的误差差异。

**有 GPU 的路径**（需要 sm_80/sm_90 级别的卡，如 A100/H100/RTX 4090，且已按 u1-l2 完成安装）：

1. 操作步骤：
   - 进入仓库根目录，运行 `python evaluation/test.py`（若报 `ModuleNotFoundError: bit_decode_cuda`，先确认 `pip show bitdecode` 能找到包、以及 `import bit_decode` 是否成功）。
   - 记录前几行输出：`residual_len: 0, residual: False, seqlen_k_pack: 1024`，以及每轮的 `cur_residual_len: 1..32`。
   - 把 32 行 `Round N: bitdecode vs pytorch: ...` 的 MAE 抄进表格或画成折线。
   - 修改 [evaluation/test.py:42](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L42) 的 `num_bits = 4` 为 `2`（`group_size` 保持 32，`pack_nums`、各张量形状会自动随 `16/num_bits` 变化），重新运行并记录新的误差曲线。
2. 需要观察的现象：MAE 在 32 轮内基本平稳（主缓存量化误差固定，残余区是精确 FP16，新 token 只占很小权重）；2-bit 的 MAE 显著大于 4-bit。
3. 预期结果：4-bit 与 2-bit 的具体数值待本地验证；定性结论是 2-bit 误差数倍于 4-bit——每 2 bit 只能表示 4 个电平，量化步长更大。
4. 注意：`test.py` 第 5 行 `import triton`，需要环境里有 triton（通常随 `pip install torch` 附带；若缺失需单独安装）。

**无 GPU 的替代路径**：通读 `test.py`，写出下表（答案已按默认配置 `num_bits=4, group_size=32` 填好，可作为自检）：

| 步骤 | 代码行 | 张量 | 形状变化 |
|---|---|---|---|
| prefill 生成 | L64-66 | `q` / `k_state` / `v_state` | `(1,1,32,128)` / `(1,1024,32,128)` ×2 |
| 残余切分 | L68-70 | `residual_len` | `1024 % 128 = 0`，无残余 |
| 打包分配 | L78-82 | `k_pack` 等 4 个 | 见 4.2.3 的表格 |
| 打包后 | L97-107 | 主缓存 | `k_pack (1,256,32,128)` 等 |
| new 缓冲 | L110-113 | `k_pack_new` 等 | `(1,32,32,128)` / `(1,4,32,128)` / `(1,128,32,32)` / `(1,4,32,128)` |
| decode 每轮 | L117-118 | `k_new`/`v_new` | `(1,1,32,128)` |
| 残余追加 | L129 | `k_residual_cache` | 长度 1→2→…→32 |
| 补零缓冲 | L127-135 | `k_residual` | 恒为 `(1,128,32,128)`，前 `cur_residual_len` 行有效 |
| 参考追加 | L156-157 | `k_state` | `(1,1024+n,32,128)`，n=轮次 |

#### 4.2.5 小练习与答案

**练习 1**：默认配置下第几轮 decode 会第一次触发 [evaluation/test.py:152-154](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L152-L154) 的拼回分支？`range(32)` 的循环里它会发生吗？

**答案**：初始残余为空（1024 整除 128），每轮残余 +1，所以第 128 轮 decode（`round_idx=127`）第一次满足 `cur_residual_len == 128`。`range(32)` 只跑到 32，永远不会触发。

**练习 2**：把循环改成 `range(130)`（在你自己的 checkout 里改），`cur_residual_len` 的打印会呈什么规律？主缓存形状如何变？

**答案**：呈锯齿形：1, 2, …, 128，然后第 129 轮清空回到 1，再涨到 128（第 130 轮前后完成第二次拼回）。每次拼回后 `v_pack.shape[1]` 增加 128，`k_pack.shape[1]` 增加 `128/pack_nums`。此行为待本地验证。

**练习 3**：为什么 q 可以 32 轮不变，而参考实现的 `k_state`/`v_state` 每轮都要 `torch.cat`？

**答案**：decode 每步的输入 query 就是「上一个生成的 token」；测试里简化为固定 q，重点考察 KV cache 的增长路径。参考实现没有缓存概念，只能每轮把全量 KV 拼起来重算，这也解释了朴素实现的 \(O(S^2)\) 总开销。

### 4.3 DynamicCache 的 update_pack / update_residual / clear_residual

#### 4.3.1 概念说明

`test.py` 里所有缓存的读写都经过 `bit_decode.models.cache_utils.DynamicCache`——它是 HF transformers 同名类的改造版。与原版相比，它把「一组 key/value 列表」扩成「六组列表」：原 `key_cache`/`value_cache` 被复用为 **FP16 残余缓存**，新增的 `key_cache_pack`/`key_cache_params`/`value_cache_pack`/`value_cache_params` 存放**低比特主缓存**。三个新方法是残余机制的全部状态机：`update_residual` 负责增长，`update_pack` 负责主缓存的读与写，`clear_residual` 负责清空。

#### 4.3.2 核心流程

```
update_residual(k, v, layer)   → key_cache[layer] = cat(key_cache[layer], k, dim=-3)   # 残余 +1 token
update_pack(kp, kq, vp, vq, l) → key_cache_pack[l] = cat(..., kp, dim=-3)  # 写：拼一个新块
                                v_params 特例：cat(..., dim=-1)            # seq 在最后一维
update_pack(None×4, layer)     → 只读：跳过写分支，返回当前 4 个张量
clear_residual(layer)          → key_cache[layer] = []，value_cache[layer] = []
```

残余机制的完整生命周期（状态机视角）：

```
[残余长度 0] --每轮 update_residual--> [1..127] --到达 128--> kernel 量化写出 *_new
      ↑                                                        |
      +------------------ clear_residual <---- update_pack(*_new) 拼回主缓存
```

#### 4.3.3 源码精读

**六组缓存列表**：[bit_decode/models/cache_utils.py:465-474](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L465-L474) 在 `__init__` 中声明 `key_cache`、`key_cache_pack`、`key_cache_params`、`value_cache`、`value_cache_pack`、`value_cache_params` 六个列表（每层一个元素）。

**update_residual**：[bit_decode/models/cache_utils.py:559-605](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L559-L605)。残余张量布局是 `(b, seqlen, h, d)`，所以追加在 `dim=-3`（[bit_decode/models/cache_utils.py:602-603](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L602-L603)）。首次调用（列表为空）直接 append 整个张量。

**update_pack**：[bit_decode/models/cache_utils.py:607-662](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L607-L662)。两个要点：

- **只读用法**：整个写逻辑被 [bit_decode/models/cache_utils.py:633](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L633) 的 `if key_pack is not None:` 包住，而 [evaluation/test.py:121](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L121) 正是传四个 `None` 只触发返回——这就是「读缓存也走同一个方法」的小技巧。
- **非对称拼接**：[bit_decode/models/cache_utils.py:657-660](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L657-L660) 中 `k_pack`/`v_pack`/`k_params` 沿 `dim=-3`（各自的 seq 维）拼接，唯独 `v_params` 沿 `dim=-1`——因为 v-tensor 布局里 seq 在最后一维（回顾 4.2.3 的形状表）。拼完还调用 `.contiguous()` 保证内存连续。

**clear_residual**：[bit_decode/models/cache_utils.py:664-666](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L664-L666) 把该层的残余直接置为空 Python 列表 `[]`（而不是空张量），下一轮 `update_residual` 走「列表为空则 append」的分支重新开始累积。

#### 4.3.4 代码实践

**实践目标**：在 CPU 上单独驱动这个微型状态机，验证你对三个方法的理解（无需 GPU、无需 `bit_decode_cuda`）。

1. 操作步骤（示例代码，非项目原有）：

   ```python
   # 示例代码：CPU 上验证 DynamicCache 三方法的状态机
   import torch
   from bit_decode.models.cache_utils import DynamicCache

   cache = DynamicCache()
   layer = 0
   # 模拟 prefill：写入主缓存（用全零假扮打包张量，只看形状）
   cache.update_pack(torch.zeros(1, 8, 2, 128), torch.zeros(1, 64, 2, 128),
                     torch.zeros(1, 1024, 2, 32), torch.zeros(1, 4, 2, 1024), layer)
   # 模拟 3 轮 decode：残余追加
   for _ in range(3):
       kr, vr = cache.update_residual(torch.zeros(1, 1, 2, 128), torch.zeros(1, 1, 2, 128), layer)
   print(kr.shape)                    # 预期 (1, 3, 2, 128)
   print([t.shape for t in cache.update_pack(None, None, None, None, layer)])  # 只读
   cache.clear_residual(layer)
   print(cache.update_residual(torch.zeros(1, 1, 2, 128), torch.zeros(1, 1, 2, 128), layer)[0].shape)
   # 预期 (1, 1, 2, 128)：清空后重新从 1 开始
   ```

   注意：`bit_decode/__init__.py` 会级联导入 `bit_decode_cuda`，若扩展未编译，可直接把 `cache_utils.py` 的 `DynamicCache` 类定义复制到独立脚本中运行。
2. 需要观察的现象：残余长度线性增长；`clear_residual` 后从 1 重新计数；`update_pack(None,...)` 返回的主缓存形状不变。
3. 预期结果：与注释中的预期形状一致。CPU 可完成；若不便运行，形状推导即答案。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `update_residual` 拼接用 `dim=-3`，而 HF 原版 `DynamicCache.update` 用 `dim=-2`？

**答案**：两者张量布局不同。HF 原版缓存是 `(b, h, s, d)`，seq 在 `dim=-2`；BitDecoding 的残余张量沿用 `(b, s, h, d)`（与 kernel 接口一致），seq 在 `dim=-3`。对比 [bit_decode/models/cache_utils.py:554-555](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L554-L555) 与 [bit_decode/models/cache_utils.py:602-603](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L602-L603) 可以同时看到这两种布局。

**练习 2**：`update_pack` 里为什么 `v_params` 的拼接维和其他三个不同？

**答案**：`v_params` 的形状是 `(b, d/group_size, h, seqlen)`，序列维在最后一维（`dim=-1`），这是 V 逐张量量化沿 head_dim 分组的布局决定的；其余三个张量的 seq 维都在 `dim=-3`。

**练习 3**：`clear_residual` 把列表元素设为 `[]` 而不是 `torch.tensor([])`，会破坏什么兼容性又换来了什么？

**答案**：换来的是 `update_residual` 里 `len(self.key_cache[layer_idx]) == 0` 的判断成立、直接 append 重新累积；代价是原版 `update`/`get_seq_length` 里对张量调用 `.numel()` 的代码路径遇到 `[]` 会出错——所以残余缓存不能混用原版 `update` 方法，这也是模型层（u6-l2）必须整体替换注意力类的原因之一。

### 4.4 example.py 与 example.sh：把后端接进真实模型

#### 4.4.1 概念说明

`test.py` 证明 kernel 数值正确，`example.py` 证明**整个模型用低比特 KV cache 还能算对题**。它加载 Qwen3-8B 或 Llama-3.1-8B，把 15 道 GSM8K 问答拼成约两千 token 的长提示，用 `model.generate` 生成 125 个新 token，考察量化后答案是否仍正确。命令行参数就是控制量化配置与注意力后端的总开关。

#### 4.4.2 核心流程

```
1. 猴子补丁：transformers.cache_utils 的三个缓存类换成 bit_decode 版本
2. 解析参数 → 推导 group_size 默认值（2bit→32，4bit→128）
3. 按模型名选 LlamaConfig/Qwen3Config，注入量化配置字段
4. 加载模型（fp16）+ tokenizer
5. GSM8K 拼长提示 → tokenize（截断到 max_length）
6. model.generate(max_new_tokens=125) → 解码打印新生成部分
```

#### 4.4.3 源码精读

**猴子补丁**：[evaluation/example.py:8-12](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L8-L12) 先 `from bit_decode import DynamicCache, StaticCache, Cache`，然后直接覆盖 `transformers.cache_utils` 模块里的三个同名类。这样 `model.generate` 内部创建缓存时会拿到改造版，官方生成流程零改动接入（机制细节在 u6-l1 展开）。

**命令行参数**：[evaluation/example.py:22-28](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L22-L28) 定义五个参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--model_path` | 必填 | 模型名或本地路径，按是否含 `"Llama"`/`"Qwen"` 字符串选配置类与模型类 |
| `--max_length` | 131072 | 提示截断上限 |
| `--num_bits` | 4 | 量化位宽，2 或 4 |
| `--quant_mode` | k-channel | K 的量化模式 |
| `--group_size` | None | 分组大小；不填则 [L34-35](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L34-L35) 推导：2bit→32，否则 128 |
| `--attn_backend` | flash_attention_2 | 注意力后端：`flash_attention_2` / `flash_decoding` / `bit_decoding` |

**config 注入**：[evaluation/example.py:42-47](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L42-L47) 把 `attn_backend`、`num_bits`、`quant_mode`、`group_size`、`residual_block_size`（4bit→128，2bit→256）挂到 config 上。注意 `config._attn_implementation = "flash_attention_2"` 是**恒定**的——prefill 阶段始终用 flash-attn，`attn_backend` 只决定 decode 阶段走哪条路径（[evaluation/example.py:43](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L43)）。

**长提示构造**：[evaluation/example.py:74-79](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L74-L79) 拼接 GSM8K 训练集前 15 条「Question…Answer…」再追加第 16 个问题；[evaluation/example.py:90-95](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L90-L95) 调 `model.generate(..., max_new_tokens=125)`；[evaluation/example.py:100](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L100) 只打印新生成部分。

**一键脚本**：[evaluation/scripts/example.sh:1-7](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/scripts/example.sh#L1-L7) 用 `Qwen/Qwen3-8B`、`max_length 131072`、`num_bits 4`、`k-channel`、`group_size 128`、`attn_backend bit_decoding` 运行；行尾注释列出另外两个后端，[evaluation/scripts/example.sh:10-11](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/scripts/example.sh#L10-L11) 注释里列出两个可选模型。README 的 Quick Start（[README.md:26-31](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L26-L31)）要求先 `cd evaluation` 再 `bash scripts/example.sh`——因为 [evaluation/example.py:14-15](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L14-L15) 是 `from llama import ...` 这种本地相对导入。

**依赖提醒**：[evaluation/example.py:17](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L17) 需要 `datasets` 库（加载 GSM8K），但 [requirements.txt](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/requirements.txt#L1-L7) 里没有它，首次运行前需 `pip install datasets`；模型加载还需 HF 登录凭证（Llama 系列是 gated 模型）。

#### 4.4.4 代码实践

**实践目标**：用不同量化配置与后端跑同一个 GSM8K 提示，观察生成质量。

1. 操作步骤（需 GPU + 已下载模型权重）：
   - `cd evaluation && bash scripts/example.sh`，等待生成完成，记录第 16 题的答案是否算对（原题答案：每盒 5 支铅笔）。
   - 分别改参数重跑：`--num_bits 2`（group_size 自动变 32、residual_block_size 自动变 256）、`--attn_backend flash_attention_2`（不量化的基线）、`--model_path meta-llama/Llama-3.1-8B-Instruct`。
   - 对比四种配置的输出文本。
2. 需要观察的现象：`bit_decoding` 4-bit 的答案应与 `flash_attention_2` 基本一致；2-bit 可能出现数值或推理偏差。
3. 预期结果：具体生成文本待本地验证；定性预期是 4-bit 端到端无损、2-bit 有可见退化。单卡 8B fp16 模型约需 16GB 显存。
4. 无 GPU 时的替代实践：写出「参数 → 生效值」推导表。例如给出 `--num_bits 2 --group_size 64`，应能推出：`pack_nums=8`、`residual_block_size=256`、K 打包张量 seq 维压缩 8 倍、`quant_mode` 仍为默认 `k-channel`。

#### 4.4.5 小练习与答案

**练习 1**：`example.py` 里 `config._attn_implementation` 和 `config.attn_backend` 都与注意力有关，为什么不合并成一个开关？

**答案**：`_attn_implementation` 是 HF transformers 的标准字段，控制模型默认注意力实现，这里恒为 `flash_attention_2` 以服务 prefill；`attn_backend` 是 BitDecoding 自定义字段，由改造版注意力类读取，决定 decode 阶段走 `bit_decoding`（低比特）还是 `flash_decoding`（FP16 基线）。两个阶段需求不同，所以分开。

**练习 2**：如果 `--group_size` 不传且 `--num_bits 2`，最终 group_size 是多少？residual_block_size 呢？

**答案**：[evaluation/example.py:34-35](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L34-L35)：group_size=32；[evaluation/example.py:47](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L47)：residual_block_size=256。注意这与 `test.py` 不同——`test.py` 的 `residual_block_size` 是硬编码 128 的变量（[evaluation/test.py:45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L45)），改 `num_bits=2` 时它不会自动变。

**练习 3**：为什么需要在 `from llama import ...` **之前**就完成猴子补丁（[evaluation/example.py:8-15](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L8-L15) 的顺序）？

**答案**：`llama.py`/`qwen3.py` 内部会 `import transformers` 并使用其缓存工具。Python 的 import 是模块级单例，先替换 `transformers.cache_utils.DynamicCache` 再导入模型代码，模型代码运行时 `from transformers.cache_utils import DynamicCache` 拿到的才是改造版；顺序反了的话模型模块可能已绑定原版类。

## 5. 综合实践

**任务：给 `test.py` 建立一份「配置 → 行为 → 精度」实验报告。**

把本讲三个模块串起来做一次完整实验（在你自己的 checkout 中操作，需要 GPU；无 GPU 则完成推导版本）：

1. **基线运行**：原样运行 `evaluation/test.py`，记录：首行 `residual_len/residual/seqlen_k_pack`、32 个 `cur_residual_len`、32 个 MAE 值。
2. **延长循环**：把 [evaluation/test.py:116](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L116) 的 `range(32)` 改成 `range(130)`，重新运行。验证 4.2.5 练习 2 的锯齿预测：`cur_residual_len` 应在 128 处归 1；同时观察拼回发生的那一轮 MAE 有没有跳变（新 token 从 FP16 残余变成量化块，误差应略增）。
3. **位宽对比**：改 `num_bits=2` 重跑第 1、2 步，对比 4-bit 与 2-bit 的 MAE 曲线。
4. **端到端**（可选，需 8B 模型权重）：用 `example.sh` 分别以 `bit_decoding` 4-bit、2-bit 和 `flash_attention_2` 基线生成答案，检验 kernel 级误差是否传导为题目做错。
5. 产出一份表格：每行一个配置（位宽 × 循环长度 × 后端），列包含平均 MAE、最大 MAE、残余归零次数、端到端答案对错。

无 GPU 版本：完成 4.2.4 的形状变化表 + 推导出第 2 步中每次拼回发生的轮次号（128、256 附近），并写出 `num_bits=2` 时全部 8 个张量的形状。

## 6. 本讲小结

- `evaluation/test.py` 是最小闭环：`kvcache_pack_int` 完成模拟 prefill 的量化打包，`fwd_kvcache_int` 逐轮做低比特解码注意力，每轮与 `attention_ref`（einsum + softmax 的朴素 FP16 实现）比一次 MAE。
- decode 循环的固定节拍是：`update_pack(None,...)` 读主缓存 → `update_residual` 追加新 token → 残余拷入 128 行补零缓冲 → `fwd_kvcache_int` → 攒满 128 才 `update_pack(*_new)` 拼回并 `clear_residual`。
- 改造版 `DynamicCache` 用六组列表同时存低比特主缓存与 FP16 残余缓存；`update_pack` 的拼接维对 `v_params` 是 `dim=-1`、其余是 `dim=-3`，根源是 V 逐张量量化把 seq 放在了最后一维。
- 默认配置（1024 整除 128、循环 32 次）下残余永远攒不满，拼回分支不触发——读懂打印输出比「跑通」更重要。
- `example.py` 通过命令行参数（`num_bits`/`quant_mode`/`group_size`/`attn_backend`）与 config 注入控制一切；对 `transformers.cache_utils` 的猴子补丁让官方 `generate` 无感使用低比特缓存；运行前需 `cd evaluation` 并补装 `datasets`。
- 误差解读：MAE 不为零是量化的固有代价；预期 2-bit 显著大于 4-bit，且残余机制保证最新 token 始终精确。

## 7. 下一步学习建议

下一讲进入第二单元：**u2-l1「量化基础：k-channel 与 k-tensor 两种模式及 pack/params 张量布局」**，把本讲 4.2.3 那张形状表背后的布局规则讲透——K 为什么沿 seq 分组、V 为什么沿 head_dim 分组、`pack_nums=16/num_bits` 的压包粒度如何决定索引数学。之后 **u2-l2** 专门展开本讲只初见轮廓的残余机制（`residual_block_size` 与 kernel block 的对应关系）。若你想先看模型侧，也可跳到 u6 单元对照 `evaluation/llama.py` 中与本讲 `test.py` 几乎同构的 decode 分支。建议同时把 `evaluation/test.py` 保持在编辑器里——它是后续所有 kernel 单元的对照程序。
