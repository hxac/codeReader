# u2-l2 残余（residual）机制：为什么最近 token 保留 FP16

## 1. 本讲目标

上一讲（u2-l1）我们搞清了低比特 KV cache 的张量布局：`k_pack`/`v_pack` 装打包后的整数字节，`k_params`/`v_params` 装每组的 scale/zero。但留了一个问题没有回答：**如果所有 KV 都量化了，刚生成的最新 token 怎么办？**

本讲结束后，你应该能够：

1. 解释 residual（残余）机制解决什么问题：为什么最新的 token 必须以 FP16 原精度保存，而不是立刻量化。
2. 说出 `residual_block_size` 取 128（4-bit）/ 256（2-bit）的由来——它与 CUDA kernel 模板里的 `kBlockN_pack` / `kBlockN_residual` 是同一个值。
3. 逐行读懂 `DynamicCache` 的三个新方法：`update_residual`、`update_pack`、`clear_residual`，以及它们在 decode 循环里的协作时序。
4. 手工推导「第几轮 decode 会触发残余区攒满、量化拼回主缓存」，并用 `evaluation/test.py` 验证。

## 2. 前置知识

### 2.1 为什么不能把所有 token 都量化

回顾 u1-l1 的结论：decode 阶段是 memory-bound，低比特 KV cache 的意义是把读取字节降到 1/4（int4）或 1/8（int2）。但量化是有损的：

- 量化误差对注意力的影响不是均匀的。softmax 权重最大的那些 key/value（通常是**离当前 query 最近的 token**）如果带有量化误差，会被直接放大进输出。
- 更致命的是**新 token 的 chicken-and-egg 问题**：decode 每步只产生 1 个新 token。如果每个新 token 都单独量化，一个量化组（group_size=32 或 128）里只有 1 个真实样本，max/min 统计毫无意义，scale 会极不稳定。

所以几乎所有低比特 KV cache 方案（包括 HF 官方的 `QuantizedCache`、KIVI 论文）都用同一个思路：**老 token 量化省带宽，最近若干 token 保 FP16 精度**。BitDecoding 把这个「最近若干」固定为一个块的大小，即 `residual_block_size`。

### 2.2 「攒满一块再量化」为什么是免费的

额外福利：当残余区攒满一个块（4-bit 是 128 个 token）时，BitDecoding **不额外启动一个量化 kernel**。解码时的 residual kernel 本来就要把残余区以 FP16 读进寄存器参与注意力计算（见 u1-l3 的三 kernel 调用链），此时数据已经在手边，顺手在 kernel 内部做量化打包并写出即可。本讲第 4.4 节会带你看 kernel 里那两处 `if (params.new_lens == residual_block_size)` 判断。

### 2.3 你需要记住的布局事实（承接 u2-l1）

| 张量 | 形状（k-channel） | dtype | 序列维 |
|---|---|---|---|
| `key_cache` / `value_cache`（残余区，FP16） | `(b, s_residual, h, d)` | float16 | dim=-3 |
| `key_cache_pack` | `(b, s/pack_nums, h, d)` | uint16 | dim=-3 |
| `key_cache_params` | `(b, s/group_size, h, d)` | float32 | dim=-3 |
| `value_cache_pack` | `(b, s, h, d/pack_nums)` | uint16 | dim=-3 |
| `value_cache_params` | `(b, d/group_size, h, s)` | float32 | **dim=-1** |

注意 BitDecoding 的 KV 布局是 `(batch, seqlen, heads, dim)`——序列在 dim=-3，这与 HuggingFace 惯例 `(batch, heads, seqlen, dim)`（序列在 dim=-2）不同。这个差异直接解释了下面三个 update 方法里 `torch.cat` 的维度选择。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [bit_decode/models/cache_utils.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py) | 改造版 `DynamicCache`：本讲主角，三个 update/clear 方法都在这里 |
| [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py) | kernel 正确性测试：完整演示 prefill 切分残余区 + 32 轮 decode 的 residual 生命周期 |
| [csrc/bit_decode/src/include/kernel_traits.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h) | `residual_block_size = num_bits == 4 ? 128 : 256` 的定义处，解释取值由来 |
| [csrc/bit_decode/src/flash_fwd_kernel.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h) | residual kernel：攒满一块时在 kernel 内原位再量化（本讲只看触发条件，精读留给 u5-l4） |
| [bit_decode/__init__.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/__init__.py) | 导出 `DynamicCache`，说明测试脚本如何拿到这个类 |

## 4. 核心概念与源码讲解

### 4.1 双区缓存：一块 KV cache，两种精度

#### 4.1.1 概念说明

改造版 `DynamicCache` 把「一份 KV cache」拆成了两个区：

- **低比特主缓存区**：`key_cache_pack` / `key_cache_params` / `value_cache_pack` / `value_cache_params` 四组列表（每个 decoder 层一个元素），存放量化打包后的老 token。它是带宽敏感的，decode kernel 从这里按 uint16/fp32 读。
- **FP16 残余区**：直接复用了原有的 `key_cache` / `value_cache` 两个列表！这是本仓库一个很省事的设计——原 HF `DynamicCache` 的 FP16 存储被「降级」为只存最新未满一块的 token，语义变了，字段没变。

所以 `__init__` 里一共有 **6 个列表**。残余区任何时刻满足：

\[
0 \le s_{\text{residual}} \le B_r, \quad B_r \triangleq \text{residual\_block\_size}
\]

decode 每步追加 1 个 token，残余区长度 +1；攒到 \( B_r \) 就被 kernel 量化写回主缓存，残余区清零，周而复始。

#### 4.1.2 核心流程

一个完整生成过程的时间线：

```text
prefill (S 个 prompt token)
 ├─ residual_len = S mod B_r
 ├─ 前 S - residual_len 个 token → kvcache_pack_int 量化 → update_pack 写入主缓存
 ├─ 后 residual_len 个 token → update_residual 存入 FP16 残余区
 └─ 预分配 4 个 *_new 输出 buffer（形状按 B_r 计算）

decode 第 r 轮（每轮 1 个新 token）
 ├─ update_residual(k_new, v_new)        残余区长度 +1 → cur_residual_len
 ├─ update_pack(None × 4)                【当 getter 用】读出主缓存 4 个张量
 ├─ 残余区拷进补零对齐的固定形状 buffer（形状恒为 B_r）
 ├─ fwd_kvcache_int(...)                 kernel 计算注意力；
 │        若 cur_residual_len == B_r，kernel 顺带把残余区量化写进 *_new
 ├─ 若 cur_residual_len == B_r：
 │        update_pack(k_pack_new, ...)   拼回主缓存
 │        clear_residual()               残余区清空
 └─ 与 FP16 参考实现比对误差
```

#### 4.1.3 源码精读

先看 6 个列表的初始化：

[bit_decode/models/cache_utils.py:465-474](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L465-L474) —— `DynamicCache.__init__` 在原版 `key_cache`/`value_cache` 之外新增了 `key_cache_pack`/`key_cache_params`/`value_cache_pack`/`value_cache_params` 四组列表；原 FP16 列表的语义从此变为「残余区」。

再看 test.py 里 prefill 阶段如何把序列切成两段：

[evaluation/test.py:68-72](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L68-L72) —— 计算 `residual_len = seqlen_k % residual_block_size`：整除余数决定了多少个尾部 token 不参与打包。注意当前文件里 `seqlen_k = 1024`、`residual_block_size = 128`，余数为 0，所以默认配置下 prefill 后残余区是空的。

[evaluation/test.py:87-95](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L87-L95) —— `residual` 为真时，序列尾部 `residual_len` 个 token 切出来走 `update_residual`（FP16），前段走打包；为假时整段打包。

[evaluation/test.py:110-113](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L110-L113) —— prefill 末尾按 `residual_block_size` 预分配 4 个 `*_new` 输出 buffer（如 `k_pack_new` 形状为 `(b, B_r/pack_nums, h, d)`）。它们是给 kernel「攒满一块时写再量化结果」准备的固定形状容器，之后每轮 decode 复用。

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，直观感受「双区」的形状与生命周期。

**操作步骤**（示例代码，可在任意装了 PyTorch 的机器上运行，无需编译 bit_decode_cuda）：

1. 新建 `residual_shape_demo.py`，粘贴下面的代码：

```python
# 示例代码：仅用 torch.zeros 模拟双区缓存的形状变化，不调用 bit_decode
import torch

b, h, d = 1, 32, 128
num_bits, group_size, B_r = 4, 32, 128
pack_nums = 16 // num_bits  # 4

S = 1000                    # prefill 长度
residual_len = S % B_r      # 104
seqlen_pack = S - residual_len

# 主缓存区（prefill 后）
k_pack   = torch.zeros((b, seqlen_pack // pack_nums, h, d), dtype=torch.uint16)
k_params = torch.zeros((b, seqlen_pack // group_size, h, d), dtype=torch.float32)
# FP16 残余区（prefill 后）
k_residual = torch.zeros((b, residual_len, h, d), dtype=torch.float16)
print("prefill 后:", k_pack.shape, k_params.shape, k_residual.shape)

# 模拟 decode 第 24 轮（residual_len + 24 == B_r）
k_residual = torch.cat([k_residual, torch.zeros(b, 24, h, d, dtype=torch.float16)], dim=-3)
assert k_residual.shape[1] == B_r
# 攒满 → 量化拼回：主缓存的打包行数增加 B_r // pack_nums
k_pack   = torch.cat([k_pack,   torch.zeros(b, B_r // pack_nums,   h, d)], dim=-3)
k_params = torch.cat([k_params, torch.zeros(b, B_r // group_size, h, d)], dim=-3)
k_residual = []            # clear_residual
print("拼回后:", k_pack.shape, k_params.shape, "residual 已清空")
```

2. 运行 `python residual_shape_demo.py`。

**需要观察的现象**：prefill 后 `k_pack` 的 dim=1 是 `896/4=224`、`k_params` 是 `896/32=28`、残余区是 104；拼回后分别变为 `224+32=256`、`28+4=32`，残余区清空。

**预期结果**：数字完全对上——因为 `update_pack` 的拼接量就由 \( B_r / \text{pack\_nums} \) 和 \( B_r / \text{group\_size} \) 决定。若你把 `num_bits` 改成 2，注意 `B_r` 应改为 256 才能保持整除关系（原因见 4.4 节）。

#### 4.1.5 小练习与答案

**练习 1**：FP16 残余区最多额外占用多少显存？以 4-bit、Llama-3.1-8B（32 层、8 个 KV 头、head_dim=128）、batch=1 为例。

**答案**：残余区容量是 \( 2 \times 32 \text{层} \times B_r \times h_{kv} \times d \times 2\text{字节} = 2 \times 32 \times 128 \times 8 \times 128 \times 2 \approx 16.8\,\text{MB} \)（K 和 V 各一份）。对比整个 4-bit 主缓存（同样口径下每 token 约 2×32×8×128×(4/8+32/32/…) 字节），残余区是一个与上下文长度无关的常数开销——这正是「按块对齐」的设计收益。

**练习 2**：如果不设残余区、每个新 token 立刻单独量化，group_size=32 时会发生什么？

**答案**：一个量化组里只有 1 个有效样本和 31 个补零样本，组内 max/min 退化成该样本自身，scale 被压到极小，后续 token 加入组后又剧烈跳变；同时打包写回需要处理未满 pack_nums/group_size 的碎片，kernel 无法按整块 tile 处理。按块攒满再量化同时解决了数值稳定性和对齐两个问题。

### 4.2 `update_residual`：FP16 残余区的追加

#### 4.2.1 概念说明

`update_residual` 是残余区的唯一写入入口：decode 每轮把当前层的 `k_new`/`v_new`（形状 `(b, 1, h, d)`）追加到 `key_cache`/`value_cache` 的层元素上。它与原版 `update` 长得几乎一样，但有两个为 BitDecoding 定制的差异。

#### 4.2.2 核心流程

```text
update_residual(k_new, v_new, layer_idx)
 ├─ 该层首次写入？ → 直接 append
 ├─ 该层元素为空（[] 或 numel==0）？ → 直接赋值
 └─ 否则 → torch.cat([旧, 新], dim=-3)   # 注意是 -3，不是 HF 的 -2
```

#### 4.2.3 源码精读

[bit_decode/models/cache_utils.py:559-605](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L559-L605) —— `update_residual` 全文。三个分支：新层 append（L589-595，跳过的层用空列表 `[]` 填充）、空层直接赋值（L596-600）、常规追加（L601-603）。

对比原版 `update` 的追加语句：

[bit_decode/models/cache_utils.py:554-555](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L554-L555) —— 原版沿 **dim=-2** 拼接，因为 HF 布局是 `(b, h, s, d)`。

[bit_decode/models/cache_utils.py:602-603](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L602-L603) —— `update_residual` 沿 **dim=-3** 拼接，因为 BitDecoding 的 KV 布局是 `(b, s, h, d)`。**两个方法 cat 的维度不同，是两套布局并存于同一个类里的硬证据。**

另一个细节是判空方式：原版用 `not t.numel()`（只适用于张量），`update_residual` 用 `len(...) == 0`（L597）。这不是风格偏好——`clear_residual` 会把层元素置成 Python 空列表 `[]`（见 4.4 节），`len([]) == 0` 成立而 `[].numel()` 会直接抛 `AttributeError`。判空方式必须兼容「清空后的列表」这一状态。

在 test.py 中的调用点：

[evaluation/test.py:127-135](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L127-L135) —— 每轮 decode 先分配一个**恒为 `residual_block_size` 长度、补零**的 `k_residual`/`v_residual`，再 `update_residual` 追加新 token，最后把缓存中的残余区拷到补零 buffer 的前缀。kernel 因此永远看到固定形状 `(b, B_r, h, d)` 的输入，有效长度由 `cur_residual_len`（L131，即返回的 `k_residual_cache.shape[1]`）单独告知。这就是 u1-l4 提过的「补零对齐」的原始形态。

#### 4.2.4 代码实践

**实践目标**：验证「补零对齐」前后 kernel 输入的形状不变、有效长度可变。

**操作步骤**：

1. 通读 [evaluation/test.py:116-160](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L116-L160) 的 decode 循环。
2. 注意 L132 已经自带 `print(f"cur_residual_len: {cur_residual_len}")`。
3. 把 L123 的 `seqlen_pack = v_pack.shape[1]` 附近加一行打印（示例代码）：

```python
# 示例代码：加在 test.py L124 之后
print(f"  seqlen_pack={seqlen_pack}, k_residual.shape={tuple(k_residual.shape)}")
```

4. 有 GPU 时运行 `python evaluation/test.py`（需先完成 u1-l2 的编译安装）。

**需要观察的现象**：32 轮里 `k_residual.shape` 恒为 `(1, 128, 32, 128)`，而 `cur_residual_len` 从 1 递增到 32；`seqlen_pack` 恒为 1024 不变（默认配置下 32 轮内不会触发拼回）。

**预期结果**：形状恒定、有效长度递增。若把 `seqlen_k` 改成 1000（见第 5 节综合实践），还能观察到 `cur_residual_len` 从 105 开始递增。运行输出为「待本地验证」（本讲义编写环境无 GPU）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `update_residual` 不像原版 `update` 那样在 `layer_idx == 0` 时累加 `_seen_tokens`？

**答案**：原版用 `_seen_tokens` 支撑 `get_seq_length` 等 API；残余机制下 token 总数 = 主缓存长度 + 残余长度，跨两个存储区，单一计数器语义不再成立。源码里相应行被注释掉了（[cache_utils.py:585-587](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L585-L587)），长度信息改由张量形状直接表达（如 `cur_residual_len = k_residual_cache.shape[1]`）。

**练习 2**：`update_residual` 追加后返回什么？test.py 用返回值做了什么？

**答案**：返回 `(self.key_cache[layer_idx], self.value_cache[layer_idx])`，即**追加后的完整残余区**（不是只有新 token）。test.py L129 用它取 `cur_residual_len`，并把整段残余区拷进补零 buffer 的前缀（L134-135）。

**练习 3**：若某层被跳过未写入，`update_residual` 和原版 `update` 各用什么填充空位？

**答案**：`update_residual` 填 Python 空列表 `[]`（L592-593），原版填 `torch.tensor([])`（L544-545）。前者是为了与 `clear_residual` 置空后的状态保持同类型。

### 4.3 `update_pack`：低比特主缓存的拼接（兼作读取器）

#### 4.3.1 概念说明

`update_pack` 负责主缓存区的写入：prefill 结束时写入第一段量化结果，之后每当残余区攒满一块，再把 kernel 产出的 `*_new` 四个张量拼接上去。它还隐藏了一个实用技巧——**传 `None` 时它退化为纯读取器**。

#### 4.3.2 核心流程

```text
update_pack(key_pack, key_params, value_pack, value_params, layer_idx)
 ├─ key_pack is None？ → 跳过所有写入，直接返回现存的 4 个张量（getter 模式）
 ├─ 该层首次写入？ → 4 个列表分别 append
 └─ 否则 → 4 路 torch.cat：
      key_cache_pack    沿 dim=-3
      value_cache_pack  沿 dim=-3
      key_cache_params  沿 dim=-3
      value_cache_params 沿 dim=-1   ← 唯独它是 -1
     每路拼接后 .contiguous()
```

#### 4.3.3 源码精读

[bit_decode/models/cache_utils.py:633](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L633) —— 整个方法体包在 `if key_pack is not None:` 里，这就是 getter 模式的开关。

[evaluation/test.py:121](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L121) —— decode 每轮用 `update_pack(None, None, None, None, layer_idx)` 取回主缓存四张量，省去一个专门的 `get` 方法。紧接着 L123 用 `v_pack.shape[1]` 得到打包 token 数 `seqlen_pack`（`v_pack` 是 `(b, s, h, d/pack)` 布局，dim=1 恰是序列维）。

[bit_decode/models/cache_utils.py:657-660](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L657-L660) —— 四路拼接。K 系三个张量沿 dim=-3（序列维）增长；`value_cache_params` 却沿 **dim=-1**——因为 u2-l1 讲过，`v_params` 的布局是 `(b, d/group_size, h, s)`，序列被放到了最后一维，同组参数内存连续。拼接后 `.contiguous()` 确保拼接产生的非连续视图被物化，后续 kernel 按连续 stride 访问。

[bit_decode/models/cache_utils.py:662](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L662) —— 返回值是四元组 `(key_cache_pack, key_cache_params, value_cache_pack, value_cache_params)`，与输入参数顺序一致，方便调用方对称地接收。

#### 4.3.4 代码实践

**实践目标**：用 CPU 上的形状演算验证「攒满一块后四路 cat 各增长多少」。

**操作步骤**：

1. 复用 4.1.4 的脚本骨架，把「拼回」一段改成对四个张量同时 cat（示例代码）：

```python
# 示例代码：拼回时四个张量各自的 cat 维度与增量
b, h, d, B_r = 1, 32, 128, 128
pack_nums, group_size = 4, 32

k_pack   = torch.zeros(b, 224, h, d,             dtype=torch.uint16)   # (b, s/pack, h, d)
k_params = torch.zeros(b, 28,  h, d,             dtype=torch.float32)  # (b, s/g,   h, d)
v_pack   = torch.zeros(b, 896, h, d // pack_nums, dtype=torch.uint16)  # (b, s,     h, d/pack)
v_params = torch.zeros(b, d // group_size, h, 896, dtype=torch.float32)# (b, d/g,   h, s)

k_pack_new   = torch.zeros(b, B_r // pack_nums,  h, d,               dtype=torch.uint16)
k_params_new = torch.zeros(b, B_r // group_size, h, d,               dtype=torch.float32)
v_pack_new   = torch.zeros(b, B_r, h, d // pack_nums,               dtype=torch.uint16)
v_params_new = torch.zeros(b, d // group_size, h, B_r,               dtype=torch.float32)

k_pack    = torch.cat([k_pack,    k_pack_new],    dim=-3)
k_params  = torch.cat([k_params,  k_params_new],  dim=-3)
v_pack    = torch.cat([v_pack,    v_pack_new],    dim=-3)
v_params  = torch.cat([v_params,  v_params_new],  dim=-1)   # 唯独它是 -1
for n, t in zip("k_pack k_params v_pack v_params".split(), (k_pack, k_params, v_pack, v_params)):
    print(n, tuple(t.shape))
```

2. 运行并核对输出。

**需要观察的现象**：拼接后 `k_pack` dim=1 从 224→256、`k_params` 28→32、`v_pack` dim=1 从 896→1024、`v_params` dim=-1 从 896→1024。

**预期结果**：四个张量各自在其「序列维」上增长了恰好 \( B_r \) 个 token 对应的量。这组增量只依赖 `B_r`、`pack_nums`、`group_size`，与历史长度无关——所以 kernel 每次产出的 `*_new` 形状也是固定的（test.py L110-113 只分配一次就能每轮复用）。

#### 4.3.5 小练习与答案

**练习 1**：getter 模式下为什么不会误触发写入？

**答案**：写入逻辑整体在 `if key_pack is not None:` 守卫内（L633），四个参数同进同退；`None` 时直接落到 L662 的 return，方法变成纯读取。

**练习 2**：如果去掉 L657-660 的 `.contiguous()`，最可能出什么问题？

**答案**：`torch.cat` 本身返回新连续内存，此处 `.contiguous()` 是防御性写法；真正的风险点在于后续若有人把 cat 换成视图式追加（如 `torch.Tensor` 切片拼接）或缓存被切片后复用，非连续张量传入 CUDA 扩展时，`CHECK_CONTIGUOUS` 类断言会失败或按错误 stride 寻址。保留它保证了传入 `set_params_fprop` 的张量永远满足 kernel 对连续性的假设。

**练习 3**：`v_params` 为什么不设计成和 `k_params` 一样的 `(b, s/g, h, d)` 布局，从而四个 cat 统一用 dim=-3？

**答案**：u2-l1 的结论：V 是 tensor 量化，`v_params` 为 `(b, d/g, h, s)` 时同一量化组的 scale/zero 在内存中连续，kernel 加载参数 tile 时一次拷贝即可取齐一组；若沿用 K 的布局，同组参数会沿序列散开。代价就是 Python 侧拼接维度不统一——这是「kernel 访问效率优先于宿主代码整洁」的典型取舍。

### 4.4 `clear_residual` 与三者协作：攒满一块后的完整时序

#### 4.4.1 概念说明

`clear_residual` 只有两行，但它是生命周期闭环的最后一环：主缓存拼回后，残余区必须立刻清空，否则下一轮会把已量化的 token 再追加一遍（重复计数）。三个方法的协作由 test.py 的一个 `if` 驱动，触发条件正是 `cur_residual_len == residual_block_size`。本节同时回答学习目标 1：**128/256 这两个数不是拍脑袋定的，它们就是 kernel 模板里的 tile 常量。**

#### 4.4.2 核心流程

触发轮的判定（设 prefill 长度为 \( S \)，块大小 \( B_r \)，整除余数 \( \ell_0 = S \bmod B_r \)；注意 test.py 约定 \( \ell_0 = 0 \) 时残余区从空开始）：

\[
\ell_0 = S \bmod B_r, \qquad r^* = B_r - \ell_0 \quad (\ell_0 > 0)
\]

即第 \( r^* \) 轮 decode 结束时残余区长度首次到达 \( B_r \)。第 \( k \) 轮的残余区长度为：

\[
\ell_k = \big( \ell_0 + k - 1 \big) \bmod B_r + 1
\]

（第 \( k \) 轮 append 之后、可能的 clear 之前。）触发轮之后残余区归零重新计数。

三个方法在触发轮的时序：

```text
第 r* 轮 decode：
  1. update_residual(k_new, v_new)      → 残余区长度 == B_r
  2. update_pack(None×4)                → 读出主缓存（长度还是旧值）
  3. fwd_kvcache_int(..., cur_residual_len=B_r, ...)
       └─ kernel 内 if (new_lens == residual_block_size)
            ├─ 量化 K 残余 fragment → 写 k_pack_new / k_params_new
            └─ 量化 V 残余 fragment → 写 v_pack_new / v_params_new
  4. update_pack(k_pack_new, k_params_new, v_pack_new, v_params_new)  → 主缓存增长 B_r
  5. clear_residual(layer_idx)          → 残余区 = []，下一轮从 1 重新计数
```

#### 4.4.3 源码精读

**触发点（Python 侧）**：

[evaluation/test.py:152-154](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L152-L154) —— `if cur_residual_len == residual_block_size:` 成立时，先 `update_pack` 拼回四个 `*_new`，再 `clear_residual`。顺序不能颠倒：先清空的话 `update_pack` 虽不依赖残余区，但下一轮的 `update_residual` 会把新 token 拼到已清空的列表上，逻辑才自洽；反之若只拼回不清空，下一轮残余区长度会变成 \( B_r + 1 \)，超出补零 buffer 的容量。

**清空的实现**：

[bit_decode/models/cache_utils.py:664-666](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L664-L666) —— `clear_residual` 把该层的 `key_cache`/`value_cache` 置为 Python 空列表 `[]`（不是空张量）。这解释了 4.2 节 `update_residual` 判空必须用 `len(...) == 0` 的原因。注意一个潜在坑：`get_seq_length`（[cache_utils.py:668-677](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L668-L677)）的判空链会走到 `not self.key_cache[layer_idx].numel()`，对空列表会抛 `AttributeError`——在残余已清空、且某处调用 `get_seq_length(layer_idx)` 时是否实际触发，待本地验证（test.py 全程未调用它）。

**块大小的定义处（CUDA 侧）**：

[csrc/bit_decode/src/include/kernel_traits.h:73-75](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L73-L75) —— `pack_num = 16 / num_bits`，紧接着 `residual_block_size = num_bits == 4 ? 128 : 256`。这是唯一的定义点，Python 侧传入的 `residual_block_size` 必须与之相等（[decode_api.cpp:333](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L333) 给了默认值 128，test.py L147 显式传入）。

[csrc/bit_decode/src/include/kernel_traits.h:83-88](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L83-L88) —— 关键对应关系：`kBlockN_pack = num_bits == 4 ? 128 : 256`，且 `kBlockN_residual = kBlockN_pack`（L84）。也就是说：

\[
B_r \equiv \text{residual\_block\_size} = \text{kBlockN\_residual} = \text{kBlockN\_pack}
\]

残余区大小 = residual kernel 的一个 KV tile = 打包主缓存的一个 pack 块。三重身份合一。为什么 2-bit 恰好翻倍到 256？算一笔账：打包后一个块占据的 uint16「token 行」数是 \( \text{kBlockN\_pack} / \text{pack\_num} \)，4-bit 时 \( 128/4 = 32 \)，2-bit 时 \( 256/8 = 32 \)——**两种位宽下打包 tile 的共享内存占用完全相同**，kernel 的 `SmemLayout` 尺寸不必随 num_bits 变化。

此外 \( B_r \) 还必须同时被 `pack_num`（4 或 8，保证 uint16 容器不跨块）和 `group_size`（32 或 128，保证量化组不跨块）整除：128 和 256 都满足 \( 128 \equiv 0 \pmod{\{4,8,32,128\}} \)。这就是 `kBlockK_params_new = kBlockN_pack / group_size`（L88）能整除的前提。

**kernel 内的原位再量化（触发条件的另一端）**：

[csrc/bit_decode/src/flash_fwd_kernel.h:132-133](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L132-L133) —— residual kernel 从 `params.new_lens`（即 Python 传入的 `cur_residual_len`，见 [decode_api.cpp:462](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L462)）读残余有效长度，并计算 tile 数 `n_blocks_residual`。由于 \( B_r = \text{kBlockN\_residual} \)，残余区永远恰好一个 tile。

[csrc/bit_decode/src/flash_fwd_kernel.h:401-420](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L401-L420) —— K 的再量化：QK gemm 之后（K 的 FP16 fragment 已经在寄存器 `tSrK_residual` 里），`if (params.new_lens == residual_block_size)` 成立时按 `quant_mode` 调 `qpack_Kchannel_Vtensor` + `pack_Kchannel_store`（k-channel）或 `quant_Ktensor` + `pack_Ktensor_store`，把量化结果写出 to `gK_new_pack`/`gK_new_params`——即 Python 侧的 `k_pack_new`/`k_params_new`。

[csrc/bit_decode/src/flash_fwd_kernel.h:467-475](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L467-L475) —— V 的再量化：PV gemm 之后同样判断、调用 `qpack_Kchannel_Vtensor` + `pack_Vtensor_store`。V 恒为 tensor 量化，无 quant_mode 分支。

这就是「原位/顺带」的含义：**注意力计算本来就要把残余区读进寄存器，量化打包只是对这些已在手边的 fragment 追加一次归约和写回**，省掉了单独的 qpack kernel 启动和一次全局内存重读（原语细节留给 u4/u5）。

顺带一个真实的源码差异：CUDA 端独立测试 [test_single_residual.cu:24](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L24) 采用的约定是 `residual_len = (seqlen % B_r == 0) ? B_r : seqlen % B_r`——整除时保留**一整块**残余而非清空；而 test.py 用 `residual = residual_len > 0`（整除时残余区为空）。两处约定不同，读代码时留意。

#### 4.4.4 代码实践

**实践目标**：不写 CUDA，纯读源码推导 `residual_block_size` 的约束链并验证。

**操作步骤**：

1. 打开 [kernel_traits.h:73-93](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L73-L93)，对两组配置手算下表常量：

| 常量 | 表达式 | num_bits=4, group_size=32 | num_bits=2, group_size=32 |
|---|---|---|---|
| `pack_num` | 16/num_bits | 4 | 8 |
| `residual_block_size` | — | ? | ? |
| `kBlockN_pack` | — | ? | ? |
| `kBlockP_new_pack`（k-channel） | kBlockN_pack/pack_num | ? | ? |
| `kBlockK_params_new`（k-channel） | kBlockN_pack/group_size | ? | ? |

2. 核对：(a) `kBlockP_new_pack` 两列是否相等；(b) `residual_block_size % pack_num` 与 `% group_size` 是否全为 0。
3. 思考：若把 2-bit 的 `residual_block_size` 也定成 128，(a)(b) 哪条破坏？

**需要观察的现象**：手算结果与源码 constexpr 一致。

**预期结果**：4-bit 列为 128/128/**32**/4，2-bit 列为 256/256/**32**/8——`kBlockP_new_pack` 两列都是 32，验证了「两种位宽打包 tile 占用相同」的设计意图；整除性全部成立。若 2-bit 用 128，则 `kBlockP_new_pack = 128/8 = 16 ≠ 32`，共享内存布局随位宽缩水，swizzle/MMA 的 tile 划分都要重排——这就是翻倍到 256 的原因。此推导只依赖源码常量，可离线完成，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：test.py 的触发条件写在 Python 侧（L152），kernel 内部也有一份 `if (params.new_lens == residual_block_size)`（flash_fwd_kernel.h:401/467）。两处判断各管什么？

**答案**：kernel 内的判断管「要不要把残余 fragment 量化写进 `*_new` buffer」——只有攒满一块，块内整除关系才成立，才有合法的 pack 行和量化组可写；Python 侧的判断管「要不要消费这些 `*_new`」——执行 `update_pack` 拼回与 `clear_residual` 清空。前者是生产条件，后者是消费条件，共同保证 `*_new` 只在攒满那一轮被写、也只在那一轮被读。

**练习 2**：为什么 `clear_residual` 置空用的是 `[]` 而不是 `torch.zeros(0, ...)` 或 `None`？

**答案**：置 `[]` 后，`update_residual` 的 `len(...) == 0` 分支（L596-600）会在下一轮把新张量直接赋上去，恢复正常追加路径；若置 `None`，`len(None)` 抛 TypeError；若置空张量，则与「跳层填充用 `[]`」（L592-593）类型不一致，且原版 `.numel()` 判空和 `len()` 判空两套约定会进一步混淆。`[]` 是与 `update_residual` 的填充约定配套的最小选择。

**练习 3**：residual kernel 每轮都要跑，即使残余区只有 1 个有效 token。这浪费吗？

**答案**：不划算的部分确实存在——补零区会被加载和参与 masked 计算，这是「固定形状换 kernel 简洁」的代价；但 (1) \( B_r \) 上限只有 128/256，相对可能上千 token 的主缓存是小头；(2) masked softmax（flash_fwd_kernel.h:360 的 `mask_residual` 以 `params.new_lens` 为界）保证补零 token 不影响输出；(3) 攒满一轮的量化是顺带的，不需要额外 kernel。权衡下来整体是净赚。

## 5. 综合实践

**任务：推导并验证「第几轮触发拼回」**（对应本讲规格中的实践任务，需 GPU 才能运行验证部分）。

**第一步（推导，可离线完成）**：读 [evaluation/test.py:116-160](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L116-L160) 的 decode 循环，回答：

- **情形 A（规格假设）**：`residual_block_size=128`、`seqlen_k=1000`。prefill 时 `residual_len = 1000 % 128 = 104`，残余区从 104 个 token 起步。第 \( k \) 轮 decode 后长度为 \( 104 + k \)，令其等于 128 得 \( k = 24 \)——即**第 24 轮 decode**（`round_idx=23`，打印为 `Round 25`）首次触发 `update_pack(k_pack_new,...)` 与 `clear_residual`；默认 `range(32)` 的循环足以观察到这一次触发。触发后 `seqlen_pack` 从 896 跳到 1024，`cur_residual_len` 归 1 重新爬升。
- **情形 B（当前文件真实配置）**：`seqlen_k = 1024`（test.py L57），`1024 % 128 = 0`，残余区从空起步，要到第 128 轮才触发——超出 `range(32)`，所以 u1-l4 说「默认 32 轮测试不会触发拼回」。要看触发需把 L116 改成 `for round_idx in range(130):`。

**第二步（修改与运行，需 GPU + 已按 u1-l2 编译）**：

1. 把 L57 改为 `seqlen_k = 1000`（情形 A）。
2. L132 已有 `cur_residual_len` 打印；在 L152 的 `if` 前后各加一行（示例代码）：

```python
# 示例代码：加在 test.py L151 位置
print(f"  trigger check: cur={cur_residual_len} == {residual_block_size} ? "
      f"v_pack seq={v_pack.shape[1]}")
```

3. 运行 `python evaluation/test.py`，记录每轮的 `cur_residual_len` 与触发后 `seqlen_pack` 的跳变。

**预期结果（待本地验证）**：`cur_residual_len` 依次为 105, 106, …, 128；在 128 那轮之后变回 1, 2, …，且该轮 `v_pack.shape[1]` 从 896 变为 1024（下一轮起生效）。同时观察误差打印：触发轮前后 MAE 量级不应跳变——说明「攒满一块量化拼回」这个事件本身不破坏正确性，量化误差是平滑进入的。

**无 GPU 替代方案**：完成 4.1.4 与 4.3.4 的两个 CPU 形状演算脚本，用 `range(32)` 循环模拟 1000 起步的残余区长度序列，断言 `cur_residual_len` 序列中 128 出现的位置等于你推导的第 24 轮。

## 6. 本讲小结

- 改造版 `DynamicCache` 用 6 个列表实现「双区缓存」：4 个低比特主缓存列表 + 复用原字段的 2 个 FP16 残余区列表；残余区长度恒在 \( [0, B_r] \) 内。
- `update_residual` 沿 **dim=-3**（`(b, s, h, d)` 布局的序列维）追加新 token，判空用 `len() == 0` 以兼容清空后的 `[]` 状态。
- `update_pack` 四路拼接（K 系 dim=-3、`value_cache_params` dim=-1，承接 u2-l1 的布局结论），传 `None` 时退化为纯读取器——test.py 每轮用它取主缓存。
- `clear_residual` 把残余区置为 `[]`，与 `update_pack(*_new)` 在 `cur_residual_len == residual_block_size` 时成对出现，构成「追加 → 攒满 → 量化拼回 → 清空」的闭环。
- `residual_block_size = kBlockN_residual = kBlockN_pack`（4-bit 128、2-bit 256）：残余区恰好是 residual kernel 的一个 tile，也是打包主缓存的一个块；2-bit 翻倍到 256 使两种位宽的打包 tile 同为 32 个 uint16 行，且 128/256 同时被 pack_num 与 group_size 整除。
- 攒满一块的量化发生在解码 kernel **内部**（flash_fwd_kernel.h:401/467 的 `if (params.new_lens == residual_block_size)`），复用已在寄存器中的 FP16 fragment，无需额外 kernel 启动。

## 7. 下一步学习建议

- **u2-l3（Python 接口精读）**：本讲多次把 `cur_residual_len`、`residual_block_size` 传给 `fwd_kvcache_int`，下一讲逐参数精读这两个 API 的完整签名与 `num_bits` 分流。
- **提前浏览** [evaluation/llama.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py) 中 `LlamaBitDecoding.forward` 的 decode 分支，看模型层如何把本讲的「补零对齐 + 触发拼回」包进 attention 前向（u6-l2 精读）。
- **为 u5-l4 做准备**：本讲只标了 kernel 内再量化的触发位置；`qpack_Kchannel_Vtensor`、`pack_Kchannel_store` 等原语的归约与位运算细节在第四、五单元展开。
- 若你想深挖「整除时残余区是否该留一块」的约定差异，对照 [test_single_residual.cu:24](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/test_single_residual.cu#L24) 与 [test.py:68-69](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py#L68-L69)，思考两种约定对 `seqlens_k` 语义的影响——这会是 u7-l4 架构评审的好素材。
