# 改造版 transformers 缓存：DynamicCache 与猴子补丁

## 1. 本讲目标

学完本讲，你应该能够：

1. 列出改造版 `DynamicCache` 为每层维护的 **6 个缓存列表**（4 个新增 + 2 个复用），并说出每个列表的角色、形状与 dtype。
2. 精读 `update_residual` / `update_pack` / `clear_residual` 三个新方法，解释 `update_pack` 四路拼接中 **K 系沿 `dim=-3`、`v_params` 沿 `dim=-1`** 的非对称设计从何而来。
3. 理解 `example.py` 里三行**猴子补丁（monkey patch）**如何让官方 `model.generate()` 在完全不知情的情况下创建并使用改造版缓存，以及这套方案为什么"恰好能跑通"、边界在哪里。

本讲是第六单元（模型集成）的第一课：前五个单元我们一直在 kernel 与绑定层的视角里，本讲回到 Python 模型层，回答一个贯穿始终的问题——**低比特数据到底放在哪个对象里，HuggingFace 的生成循环为什么能无感地用它？**

## 2. 前置知识

### 2.1 HuggingFace 的 KV cache 抽象

transformers 把"注意力的历史状态"抽象成 `Cache` 对象（基类只规定 `update` / `get_seq_length` 等接口），生成式模型默认使用 `DynamicCache`：每层一个槽位，随着 token 生成不断 `torch.cat` 增长。调用 `model.generate(...)` 时通常**不传** `past_key_values`，框架会在模型 forward 内部自己 `DynamicCache()` 一个出来，逐层填充、逐轮复用。这意味着：**谁控制了 `DynamicCache` 这个类的构造点，谁就控制了整个生成过程看到的历史状态。**

### 2.2 两种导入绑定时机（本讲最重要的 Python 知识点）

- **import 期快照**：模块顶层的 `from transformers.cache_utils import DynamicCache`，在模块被加载的那一刻就把"当时的"类对象绑定进本模块的命名空间。之后再修改 `transformers.cache_utils.DynamicCache`，这个已存在的绑定**不会变**。
- **运行期解析**：函数体内写 `from transformers.cache_utils import DynamicCache`（调用时才解析），或 `transformers.cache_utils.DynamicCache(...)`（属性访问），读取的是模块对象的**当前属性**——补丁生效。

改造版缓存要"冒名顶替"官方 `DynamicCache`，就必须保证所有会**构造或 isinstance 检查**缓存的代码点，解析到的都是改造类。BitDecoding 用"复制文件 + 猴子补丁"两板斧做到这一点，见 4.3。

### 2.3 与前几讲的衔接

- **u2-l1** 已经推导过 pack/params 张量的形状（k-channel 模式、`pack_num = 16/num_bits`、`group_size` 分组）；本讲直接使用那些结论，不再重新推导。
- **u2-l2** 讲过残余机制的"为什么"（最近 token 保 FP16、攒满一块再量化）；本讲讲"在哪存、谁调用"。
- **u2-l3** 讲过 `fwd_kvcache_int` 返回的 `*_new` 四件套；本讲会看到它们在 `llama.py` 里被 `update_pack` 消费。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [bit_decode/models/cache_utils.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py) | transformers 官方 `cache_utils.py` 的**整文件分叉**（2645 行），只对 `DynamicCache` 做了外科手术式修改 | 6 个列表、3 个新方法 |
| [bit_decode/__init__.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/__init__.py) | Python 包门面 | 对外导出 `Cache, DynamicCache, StaticCache` 三件套 |
| [evaluation/example.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py) | GSM8K 端到端生成示例 | 第 8–12 行的猴子补丁、config 注入 |
| [evaluation/llama.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py) | HF `modeling_llama.py` 的复制改造版 | 缓存类的消费点：decode/prefill 双路径、类注册表 |

一句话导览：`cache_utils.py` 提供"容器"，`llama.py` 是"使用者"，`example.py` 的补丁是"接线员"。

## 4. 核心概念与源码讲解

### 4.1 DynamicCache（改造版）：每层 6 个缓存列表

#### 4.1.1 概念说明

改造版 `DynamicCache` 要同时容纳两类数据：

1. **低比特主缓存**：已经量化打包的"老 token"，以 uint16 打包数据 + fp32 量化参数的形式存放，是 decode 注意力 kernel 的主要读取对象；
2. **FP16 残余区**：最近若干 token 的高精度副本（u2-l2 讲过的 residual 机制）。

设计者没有新造一个 `BitCache` 类，而是**直接在官方 `DynamicCache` 上动手**：新增 4 个列表放低比特主缓存，把原有的 `key_cache` / `value_cache` 两个列表**复用**为 FP16 残余区。于是每层共 6 个列表：

| # | 列表 | 角色 | 形状（k-channel，每层） | dtype | 来源 |
|---|---|---|---|---|---|
| 1 | `key_cache` | FP16 残余 K | `(b, s_res, h, d)` | fp16 | 复用 |
| 2 | `key_cache_pack` | 低比特主缓存 K | `(b, s_pack/pack_num, h, d)` | uint16 | **新增** |
| 3 | `key_cache_params` | K 的 scale/zero | `(b, s_pack/group_size, h, d)` | fp32 | **新增** |
| 4 | `value_cache` | FP16 残余 V | `(b, s_res, h, d)` | fp16 | 复用 |
| 5 | `value_cache_pack` | 低比特主缓存 V | `(b, s_pack, h, d/pack_num)` | uint16 | **新增** |
| 6 | `value_cache_params` | V 的 scale/zero | `(b, d/group_size, h, s_pack)` | fp32 | **新增** |

其中 `s_res ≤ residual_block_size`（4-bit 为 128、2-bit 为 256），`pack_num = 16/num_bits`（4-bit 时一个 uint16 装 4 个值）。注意残余区的维度顺序是 `(b, s, h, d)`——**序列在 dim -3**，与官方 `(b, h, s, d)`（序列在 dim -2）不同，这个差异会在 4.2 与 4.3 反复出现。

每 token 的有效带宽开销（承接 u2-l3）：

\[ b_{\text{eff}} = \text{num\_bits} + \frac{32}{\text{group\_size}} \quad\text{（bit/元素）} \]

即 4-bit、group_size=128 时每元素约 4.25 bit，相比 FP16 的 16 bit 压缩约 3.8 倍。

#### 4.1.2 核心流程

```text
DynamicCache 对象（每个 generate 会话一个）
├── key_cache[i]            ← FP16 残余 K，i = 层号，空槽为 Python 空列表 []
├── key_cache_pack[i]       ← uint16 打包 K
├── key_cache_params[i]     ← fp32 K 参数
├── value_cache[i]          ← FP16 残余 V
├── value_cache_pack[i]     ← uint16 打包 V
└── value_cache_params[i]   ← fp32 V 参数

生命周期（与 u2-l2 的闭环一致，本讲标注方法名）：
prefill:  update_residual(尾部不满块) + update_pack(整块量化结果)
decode:   update_pack(None×4) 读取主缓存 → update_residual(新 token)
          → kernel 计算 → 攒满时 update_pack(*_new) + clear_residual()
```

#### 4.1.3 源码精读

先看构造函数——6 个列表的出生地：

[bit_decode/models/cache_utils.py:465-474](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L465-L474) 在 `__init__` 中初始化 6 个空列表：`key_cache`/`key_cache_pack`/`key_cache_params` 与 `value_cache`/`value_cache_pack`/`value_cache_params`，每个列表按层号增长一个槽位（`_seen_tokens` 保留自官方，用于 generate 计数）。

```python
self.key_cache: List[torch.Tensor] = []          # ← 复用为 FP16 残余 K
self.key_cache_pack: List[torch.Tensor] = []     # ← 新增：uint16 打包 K
self.key_cache_params: List[torch.Tensor] = []   # ← 新增：K 的 scale/zero

self.value_cache: List[torch.Tensor] = []        # ← 复用为 FP16 残余 V
self.value_cache_pack: List[torch.Tensor] = []   # ← 新增：uint16 打包 V
self.value_cache_params: List[torch.Tensor] = [] # ← 新增：V 的 scale/zero
```

再对比"没被改的"原版 `update`，理解官方布局与残余布局的差异：

[bit_decode/models/cache_utils.py:553-555](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L553-L555) 是官方 `update` 的追加分支，沿 `dim=-2` 拼接——因为官方缓存形状是 `(b, h, s, d)`，序列在倒数第二维。

```python
self.key_cache[layer_idx] = torch.cat([self.key_cache[layer_idx], key_states], dim=-2)
self.value_cache[layer_idx] = torch.cat([self.value_cache[layer_idx], value_states], dim=-2)
```

最后看一个"复制策略的副作用"：

[bit_decode/models/cache_utils.py:950](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L950) 的 `QuantizedCache`（HF 官方的 quanto/HQQ 量化缓存）继承自改造版 `DynamicCache`，其 `__init__` 调用 `super().__init__()`，因此也会带上这 6 个列表——无害，但说明本项目的集成策略是"整文件复制 + 局部手术"，而不是"继承扩展"。

顺带一提，[evaluation/example.py:1](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L1) 的首行注释是 `# LLaMA model with KIVI`——文件从 KIVI 项目一脉相承而来，"FP16 残余 + 攒满再量化"的双区缓存思想也正是 KIVI 系设计的标志。

#### 4.1.4 代码实践

**实践目标**：不跑任何模型，仅用 `torch.zeros` 验证 6 个列表的形状推导。

**操作步骤**：

1. 在装好 bit_decode 的环境里新建 `shapes_probe.py`（示例代码，非项目原有文件）：

```python
import torch
from bit_decode import DynamicCache

b, h, d, num_bits, g = 2, 8, 128, 4, 128
pack_num = 16 // num_bits                 # 4：一个 uint16 装 4 个 int4
s_pack, s_res = 1000, 64                  # 打包区 1000 token，残余区 64 token

cache = DynamicCache()
cache.key_cache.append(torch.zeros(b, s_res, h, d, dtype=torch.float16))
cache.key_cache_pack.append(torch.zeros(b, s_pack // pack_num, h, d, dtype=torch.uint16))
cache.key_cache_params.append(torch.zeros(b, s_pack // g, h, d, dtype=torch.float32))
cache.value_cache.append(torch.zeros(b, s_res, h, d, dtype=torch.float16))
cache.value_cache_pack.append(torch.zeros(b, s_pack, h, d // pack_num, dtype=torch.uint16))
cache.value_cache_params.append(torch.zeros(b, d // g, h, s_pack, dtype=torch.float32))

for name in ["key_cache", "key_cache_pack", "key_cache_params",
             "value_cache", "value_cache_pack", "value_cache_params"]:
    t = getattr(cache, name)[0]
    print(f"{name:20s} {tuple(t.shape)} {t.dtype}")
```

2. 运行 `python shapes_probe.py`（CPU 即可，无需 GPU）。

**需要观察的现象**：6 行形状输出；特别注意 `key_cache_pack` 第 1 维是 `1000/4=250`，而 `value_cache_pack` 最后一维是 `128/4=32`、`value_cache_params` 最后一维是 1000。

**预期结果**：与 4.1.1 表格逐行一致。若不一致，回到 u2-l1 重推布局。

#### 4.1.5 小练习与答案

**练习 1**：改造版 `DynamicCache` 为每层维护几个列表？其中几个是本项目新增的？

> **答案**：6 个。新增 4 个（`*_cache_pack` / `*_cache_params`），复用 2 个（`key_cache` / `value_cache` 改作 FP16 残余区）。

**练习 2**：为什么残余区复用原 `key_cache`/`value_cache` 字段，而不是新增 `residual_cache` 列表？

> **答案**：最小改动原则。残余区在数据语义上就是 FP16 的 K/V，与原字段同类型；复用可以让 `__iter__`、`__getitem__`、`to_legacy_cache` 等官方机制保持字段名不变，`update_residual` 也能仿照 `update` 的三段式骨架来写。代价是这些方法的**语义**发生了漂移（例如 `get_seq_length` 的 `shape[-2]` 不再是序列长，见 4.3.3），属于典型的"省事但埋雷"取舍。

**练习 3**：`QuantizedCache` 继承改造版 `DynamicCache` 后会带 6 个列表吗？这暴露了什么集成策略？

> **答案**：会（`super().__init__()` 初始化）。暴露的策略是"整文件复制官方 cache_utils.py 再局部修改"，而非"写子类"——因为子类方案无法让 transformers 内部代码在 `DynamicCache()` 构造点改用你的子类，而复制+补丁可以。

### 4.2 三个新方法：update_residual / update_pack / clear_residual

#### 4.2.1 概念说明

三个方法是残余闭环（u2-l2）在容器侧的全部接口：

- **`update_residual(key, value, layer_idx)`**：把本步的 FP16 K/V 追加进残余区；
- **`update_pack(k_pack, k_params, v_pack, v_params, layer_idx)`**：把一块新量化的数据拼进低比特主缓存；传 `None` 时**兼作读取器**，返回该层现存的四元组；
- **`clear_residual(layer_idx)`**：残余攒满并拼回主缓存后，把残余区清空。

三者都不做量化——量化发生在 CUDA kernel 里（第四、五单元），容器只负责"存"与"拼"。

#### 4.2.2 核心流程

decode 一步内在 `LlamaBitDecoding.forward` 中的调用序列（详见 u6-l2，这里只看容器侧）：

```text
q_len == 1（decode 分支）：
  ① k_pack,... = update_pack(None, None, None, None, layer)   # 读取器模式：取主缓存
  ② k_res, v_res = update_residual(k_new, v_new, layer)       # 追加新 token
  ③ cur_residual_len = k_res.shape[1]
  ④ out, *_new = fwd_kvcache_int(...)                          # kernel（可能顺带再量化）
  ⑤ 若 cur_residual_len == residual_block_size：
       update_pack(k_pack_new, ..., layer)                     # 拼回主缓存
       clear_residual(layer)                                   # 清空残余区
```

#### 4.2.3 源码精读

**`update_residual` 的拼接维度**：

[bit_decode/models/cache_utils.py:601-603](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L601-L603) 沿 `dim=-3` 把新 K/V 拼进残余区——因为残余区布局是 `(b, s, h, d)`，序列在 dim -3。调用方 `llama.py` 在 [evaluation/llama.py:637-639](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L637-L639) 特意 `transpose(1, 2)` 成 `(b, q_len, h, d)` 再传入，与这里严格配套。

```python
self.key_cache[layer_idx] = torch.cat([self.key_cache[layer_idx], key_states], dim=-3)
self.value_cache[layer_idx] = torch.cat([self.value_cache[layer_idx], value_states], dim=-3)
```

[bit_decode/models/cache_utils.py:589-598](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L589-L598) 的空槽检查用 `len(...) == 0` 而非官方 `update` 里的 `.numel()`——因为 `clear_residual` 清空后槽位是 Python 空列表 `[]`（列表没有 `.numel()` 方法）。跳过的层也用 `[]` 填充，与 [bit_decode/models/cache_utils.py:543-545](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L543-L545) 官方 `update` 填 `torch.tensor([])` 的做法不同。另外 [bit_decode/models/cache_utils.py:585-587](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L585-L587) 把 `_seen_tokens` 计数注释掉了——残余追加不应重复计数。

**`update_pack` 的四路非对称拼接**（本讲学习目标的核心）：

[bit_decode/models/cache_utils.py:657-660](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L657-L660) 是四路 `torch.cat`：前三路沿 `dim=-3`（序列维），唯独 `value_cache_params` 沿 `dim=-1`，且每路都跟一个防御性的 `.contiguous()`。

```python
self.key_cache_pack[layer_idx]   = torch.cat([...key_pack],   dim=-3).contiguous()
self.value_cache_pack[layer_idx] = torch.cat([...value_pack], dim=-3).contiguous()
self.key_cache_params[layer_idx] = torch.cat([...key_params], dim=-3).contiguous()
self.value_cache_params[layer_idx] = torch.cat([...value_params], dim=-1).contiguous()
```

为什么唯独 V 参数特殊？回到 4.1.1 的形状表：

- K 系（`k_pack`、`k_params`）与 `v_pack` 的**序列都在 dim -3**，沿 -3 拼接即沿时间追加；
- `v_params` 形状是 `(b, d/group_size, h, s_pack)`，**序列被放到了最后一维**。V 是 tensor 量化，量化组沿通道 `d` 分组；把序列放 dim -1，同一量化组的 scale/zero 在内存中连续排布，decode kernel 按"组"加载参数时访存友好（kernel 侧的对应消费见 u3-l2 讲过的 `v_params` 非对称 stride 提取）。所以拼新块时自然也沿 `-1`。

**读取器模式**：

[bit_decode/models/cache_utils.py:632-633](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L632-L633) 的 `if key_pack is not None:` 是整个方法的闸门——传 `None` 时直接跳过更新，落到 [bit_decode/models/cache_utils.py:662](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L662) 返回该层四元组。调用点在 [evaluation/llama.py:649](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L649)：decode 每步先用它取出主缓存喂给 kernel，一个方法身兼"读"与"写"两职。

**`clear_residual`**：

[bit_decode/models/cache_utils.py:664-666](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L664-L666) 把该层的两个残余槽位置成空列表 `[]`。配合 4.2.3 开头说的 `len(...) == 0` 检查，下一轮 `update_residual` 会走"空槽直填"分支重新开始积累。消费点在 [evaluation/llama.py:681-683](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L681-L683)：`cur_residual_len == residual_block_size` 时先把 kernel 输出的 `*_new` 四件套 `update_pack` 拼回，再 `clear_residual`。

**闭环的另一端（prefill）**：[evaluation/llama.py:724-729](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L724-L729) 把 prefill 序列尾部不满一块的残余存进 `update_residual`；[evaluation/llama.py:734-745](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L734-L745) 先用 `kvcache_pack_int` 把整块量化进预分配张量，再 `update_pack` 入库；[evaluation/llama.py:747-750](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L747-L750) 一次性分配跨步复用的 `*_new` 缓冲。

#### 4.2.4 代码实践

**实践目标**：用最小脚本复现 `update_pack` 的四路拼接，亲眼确认"K 系 -3、V 参数 -1"拼接后的形状与连续性。

**操作步骤**：

1. 新建 `cat_probe.py`（示例代码，非项目原有文件；沿用 4.1.4 的记号，`s_pack=32`、`residual_block_size=128`）：

```python
import torch
k_old, k_new = torch.zeros(1, 8, 2, 64, dtype=torch.uint16), torch.zeros(1, 32, 2, 64, dtype=torch.uint16)
vp_old, vp_new = torch.zeros(1, 2, 2, 32), torch.zeros(1, 2, 2, 128)

k_merged = torch.cat([k_old, k_new], dim=-3)
vp_merged = torch.cat([vp_old, vp_new], dim=-1)
print("k_pack:", k_merged.shape, k_merged.is_contiguous())    # (1, 40, 2, 64)
print("v_params:", vp_merged.shape, vp_merged.is_contiguous()) # (1, 2, 2, 160)
```

2. 运行（CPU 即可）。`k_new` 第 1 维取 32 是因为一个满残余块 128 个 token、每 4 个 int4 压一个 uint16（`128/pack_num=32`）。
3. 把 `dim=-3` 改成 `dim=-1` 再跑一次，观察报错。

**需要观察的现象**：`k_pack` 拼接后第 1 维 8+32=40，对应 `s_pack` 从 32 涨到 160（32+128）；`v_params` 最后一维 32+128=160，同为 160 但维度不同；`is_contiguous()` 的返回值。

**预期结果**：形状如上；`torch.cat` 输出通常本身就是连续张量，源码里的 `.contiguous()` 更像防御性写法——**待本地验证**（打印确认即可）。错误维度拼接（步骤 3）会抛出 `RuntimeError: Sizes of tensors must match except in dimension`。

#### 4.2.5 小练习与答案

**练习 1**：`update_pack` 四路 `torch.cat` 的维度分别是什么？为什么 `v_params` 例外？

> **答案**：`-3, -3, -3, -1`。`v_params` 形状为 `(b, d/group_size, h, s_pack)`，序列在最后一维——V 按 tensor 模式沿通道分组，序列放 dim -1 使同一量化组的参数内存连续、kernel 加载友好；因此拼新块也沿 -1。

**练习 2**：调用 `past_key_value.update_pack(None, None, None, None, layer_idx)` 会发生什么？这个模式解决什么问题？

> **答案**：`if key_pack is not None` 闸门不成立，直接返回该层 `(k_pack, k_params, v_pack, v_params)` 四元组——纯读取。decode 每步（`llama.py:649`）都要把主缓存交给 `fwd_kvcache_int`，用同一方法免去了单独写 getter。

**练习 3**：`clear_residual` 置 `[]`（Python 列表）而非 `torch.tensor([])`，这与方法内哪处检查配套？有什么副作用？

> **答案**：配套 `update_residual`/`update_pack` 里的 `len(...) == 0` 空槽检查（列表可用 `len`）。副作用是官方 `get_seq_length` 的第三项检查 `not self.key_cache[layer_idx].numel()`（[bit_decode/models/cache_utils.py:674](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L674)）在元素是列表时会抛 `AttributeError`——好在该路径在 bit_decoding 的实际运行中不会被触发（见 4.3.3）。

### 4.3 example.py 猴子补丁：让官方 generate 无感接入

#### 4.3.1 概念说明

**猴子补丁（monkey patch）**指在运行时替换模块/类的属性。BitDecoding 面对的难题是：`model.generate()` 内部（以及 transformers 包内各处）会自己构造缓存、做 isinstance 检查，这些代码我们既不改也不想改。项目的解法是两板斧：

1. **复制模型文件**：把 `modeling_llama.py` 复制成 `evaluation/llama.py`，并把其中的缓存导入从官方改成 bit_decode（[evaluation/llama.py:56-57](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L56-L57)，第 57 行的官方导入被注释）——消灭了模型侧所有"import 期快照"绑定；
2. **猴子补丁**：`example.py` 开头把 `transformers.cache_utils` 命名空间里的三个类替换成 bit_decode 的同类（[evaluation/example.py:8-12](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L8-L12)）——覆盖 transformers 包内所有"运行期解析"的引用。

```python
from bit_decode import DynamicCache, StaticCache, Cache
import transformers.cache_utils
transformers.cache_utils.DynamicCache = DynamicCache
transformers.cache_utils.StaticCache = StaticCache
transformers.cache_utils.Cache = Cache
```

补丁后，凡是通过 `transformers.cache_utils.DynamicCache` 这个属性（运行期）拿到的类，与 `llama.py` 第 56 行直接导入的类是**同一个对象**——isinstance 检查与方法调用（`update_pack` 等）天然一致。替换三个类（而非只替换 `DynamicCache`）是为了保持 `Cache ← DynamicCache` 继承体系在补丁命名空间内自洽：若只换子类不换基类，`isinstance(cache, Cache)` 一类检查可能出现"子类换了、基类没换"的错位。

被补丁"骗过"的官方流程随后照常运转：[evaluation/example.py:42-47](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L42-L47) 把量化配置注入 config，[evaluation/example.py:90-95](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L90-L95) 调用 `model.generate(...)`——generate 并不知道自己每轮都在读写一个带 6 列表的低比特缓存。

#### 4.3.2 核心流程

```text
example.py 进程启动
  ① from bit_decode import Cache, DynamicCache, StaticCache   # 拿到改造类
  ② 替换 transformers.cache_utils 三个属性                     # 猴子补丁
  ③ from llama import LlamaForCausalLM                          # 复制版模型文件
       └─ 其内部 from bit_decode import Cache, ...              # 直接绑定改造类
  ④ config 注入 num_bits / quant_mode / group_size / attn_backend / residual_block_size
  ⑤ model.generate(...)
       ├─ LlamaModel.forward 内部 DynamicCache()  ←（llama.py:1048，改造类）
       ├─ 各层 LlamaBitDecoding.forward 读写 6 列表
       └─ transformers 包内运行期引用 transformers.cache_utils.* ← 一律解析到改造类
```

#### 4.3.3 源码精读

**构造点**：[evaluation/llama.py:1043-1048](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L1043-L1048) 在 `use_cache=True` 且传入的不是 `Cache` 实例时 `past_key_values = DynamicCache()`——由于 `llama.py` 顶部导入的是 bit_decode 版（[evaluation/llama.py:56](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L56)），generate 流程中诞生的正是改造版缓存。这是"复制文件"板斧的关键收益：不依赖补丁时机。

**后端选择**：[evaluation/llama.py:761-766](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L761-L766) 的 `LLAMA_ATTENTION_CLASSES` 注册表新增 `"bit_decoding"` 后端，[evaluation/llama.py:774](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L774) 按 `config.attn_backend` 实例化注意力层——只有选中该后端的层才会调用 4.2 的三个方法。

**一个必须想清楚的边界——这套方案为什么"恰好能跑通"**：改造版缓存的残余布局 `(b, s, h, d)` 与官方方法对残余列表的假设冲突。[bit_decode/models/cache_utils.py:668-677](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/models/cache_utils.py#L668-L677) 的 `get_seq_length` 沿用官方实现，取 `shape[-2]`——在残余布局下这取到的是**头数 h 而非序列长**，且清空后元素是 `[]`、`.numel()` 会抛 `AttributeError`。它之所以没炸，靠的是两个"恰好"：

- [evaluation/llama.py:1144-1147](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L1144-L1147)：`_update_causal_mask` 对 `flash_attention_2` **提前返回**，永远走不到 [evaluation/llama.py:1152](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L1152) 的 `get_seq_length()`；而 [evaluation/example.py:42](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/example.py#L42) 恰好恒设 `_attn_implementation = "flash_attention_2"`。
- [evaluation/llama.py:1057-1058](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L1057-L1058)：另一处 `get_seq_length()` 只在 `cache_position is None` 时执行——generate 循环每轮都会提供 `cache_position`，故不触发。

结论：改造版缓存目前**事实上只与 flash_attention_2 注意力实现 + generate 提供 cache_position 的路径兼容**；换 sdpa/eager 会在掩码逻辑上撞到布局漂移（具体表现依赖所装 transformers 版本，待本地验证）。

#### 4.3.4 代码实践

**实践目标**：在不修改 transformers 安装的前提下，用约 10 行复现 `example.py` 的猴子补丁，并用 `isinstance` 验证"transformers 侧构造出的缓存"已是改造版 `DynamicCache`。

**操作步骤**：

1. 新建 `patch_probe.py`（示例代码，非项目原有文件）：

```python
import transformers.cache_utils as tc
from bit_decode import Cache, DynamicCache, StaticCache      # ① 改造版三件套

tc.DynamicCache, tc.StaticCache, tc.Cache = DynamicCache, StaticCache, Cache  # ② 猴子补丁

def hf_side_construct():                                     # ③ 模拟 transformers 内部"运行期"构造点
    from transformers.cache_utils import DynamicCache as HF   #    函数内 from-import，调用时才解析
    return HF()

cache = hf_side_construct()
print(isinstance(cache, DynamicCache), hasattr(cache, "update_pack"))
```

2. 在装好 bit_decode 与 transformers 的环境运行（CPU 即可，无需模型权重）。
3. 注释掉第 ② 行再跑一次，对比输出。

**需要观察的现象**：第一次运行输出 `True True`；注释补丁后输出 `False False`。

**预期结果**：补丁生效时，"transformers 侧"（函数内 import）构造出的对象与 `llama.py` 使用的 `DynamicCache` 是同一个类，且带 `update_pack` 等三个新方法；不补丁时拿到的是官方类——若此时让 `LlamaBitDecoding` 的 decode 分支运行，会在 `update_pack(None, ...)` 处抛 `AttributeError`（官方类没有该方法）。

**进阶（需 GPU + flash-attn + 模型权重，待本地验证）**：跑一次 `example.py` 风格的 generate（`return_dict_in_generate=True`），检查返回的 `outputs.past_key_values`：`isinstance(outputs.past_key_values, DynamicCache)` 应为 `True`，且其 `key_cache_pack` 列表长度等于层数。

#### 4.3.5 小练习与答案

**练习 1**：猴子补丁为什么对"顶层 `from transformers.cache_utils import DynamicCache` 过这些类的模块"无效？

> **答案**：from-import 在**模块加载那一刻**把类对象绑定进该模块自己的命名空间，是快照；之后修改 `transformers.cache_utils.DynamicCache` 属性不会追溯改变已有绑定。正因如此，本项目才把 `modeling_llama.py` 整个复制出来改导入（`llama.py:56-57`），而不是寄希望于补丁能影响 transformers 里所有顶层绑定。

**练习 2**：`example.py` 为什么在 `from llama import ...`（第 14 行）之前先打补丁？

> **答案**：保证后续 import 链触发的任何对 `transformers.cache_utils.*` 的运行期解析都拿到改造类。虽然 `llama.py` 自己直接从 bit_decode 导入、不依赖补丁，但 import 过程中 transformers 其他模块可能在导入期或首次使用期访问这些名字，先补丁最稳妥——顺序错了不会报错，只会留下"部分引用仍是官方类"的隐患，是最难排查的一类 bug。

**练习 3**：把 `config._attn_implementation` 从 `"flash_attention_2"` 改成 `"sdpa"` 会发生什么？

> **答案**：`_update_causal_mask` 不再提前返回，会执行 `past_key_values.get_seq_length()`：残余布局 `(b, s, h, d)` 下 `shape[-2]` 返回头数（如 Llama-3.1-8B 的 KV 头数 8）而非序列长，位置编码与掩码长度计算随之错误；若残余区刚被 `clear_residual` 清成 `[]`，`.numel()` 直接抛 `AttributeError`。这正是 4.3.3 说的兼容边界（具体表现依赖 transformers 版本，待本地验证）。

## 5. 综合实践

**任务**：写一个"桌面生命周期驱动器"，不加载模型、不用 GPU，手动把一个改造版 `DynamicCache` 走完 prefill → 若干轮 decode → 攒满 → 拼回 → 清空的完整闭环，并用表格记录 6 个列表的演变。

配置：`b=1, h=2, d=64, num_bits=4`（`pack_num=4`）、`group_size=32`、`residual_block_size=128`、prefill `seqlen_k=160`（打包区 128、残余 32）。

```python
# lifecycle_probe.py（示例代码，非项目原有文件）
import torch
from bit_decode import DynamicCache

b, h, d, pack_num, g, rbs = 1, 2, 64, 4, 32, 128
cache = DynamicCache()

# ---- prefill：seqlen_k=160 → 打包 128 + 残余 32 ----
cache.update_pack(torch.zeros(b, 128//pack_num, h, d, dtype=torch.uint16),   # k_pack (1,32,2,64)
                  torch.zeros(b, 128//g, h, d),                              # k_params (1,4,2,64)
                  torch.zeros(b, 128, h, d//pack_num, dtype=torch.uint16),   # v_pack (1,128,2,16)
                  torch.zeros(b, d//g, h, 128), 0)                           # v_params (1,2,2,128)
cache.update_residual(torch.zeros(b, 32, h, d), torch.zeros(b, 32, h, d), 0) # 残余 32 token

# ---- decode：一次追加 96 token，把残余攒满到 128 ----
cache.update_residual(torch.zeros(b, 96, h, d), torch.zeros(b, 96, h, d), 0)
assert cache.key_cache[0].shape[1] == rbs

# ---- 攒满：kernel 输出的 *_new 四件套拼回，残余清空 ----
cache.update_pack(torch.zeros(b, rbs//pack_num, h, d, dtype=torch.uint16),   # k_pack_new
                  torch.zeros(b, rbs//g, h, d),                              # k_params_new
                  torch.zeros(b, rbs, h, d//pack_num, dtype=torch.uint16),   # v_pack_new
                  torch.zeros(b, d//g, h, rbs), 0)                           # v_params_new
cache.clear_residual(0)

for name in ["key_cache", "key_cache_pack", "key_cache_params",
             "value_cache_pack", "value_cache_params"]:
    print(f"{name:20s} -> {type(getattr(cache, name)[0]).__name__} "
          f"{tuple(getattr(cache, name)[0].shape)}")
```

**要求**：

1. 运行（CPU 即可）并填写下表（"清空后"一行 `key_cache` 应显示 `list`）：

| 阶段 | key_cache[0] | key_cache_pack[0] | value_cache_params[0] |
|---|---|---|---|
| prefill 后 | (1, 32, 2, 64) | (1, 32, 2, 64) | (1, 2, 2, 128) |
| 追加 96 后 | ？ | (1, 32, 2, 64) | (1, 2, 2, 128) |
| 拼回+清空后 | ？ | ？ | ？ |

2. 回答：拼回后 `key_cache_pack[0]` 第 1 维 = 32+32 = 64，对应多少个 token？`value_cache_params[0]` 最后一维应是多少？
3. 对照 [evaluation/llama.py:648-683](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L648-L683) 与 [evaluation/llama.py:724-750](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py#L724-L750)，把脚本里的每一步映射到真实调用点（注意真实 decode 每步只追加 1 个 token，脚本用 96 个一次追加只是压缩演示）。

**预期结果**：追加 96 后 `key_cache[0] = (1, 128, 2, 64)`；清空后为 `[]`（打印为 `list ()`）；拼回后 `key_cache_pack[0] = (1, 64, 2, 64)`（对应 256 个打包 token），`value_cache_params[0] = (1, 2, 2, 256)`。两处数字（64 行 pack ↔ 256 token ↔ v_params 尾维 256）互相印证"K 沿 -3、V 参数沿 -1 但都对应同一批 token"。

## 6. 本讲小结

- 改造版 `DynamicCache` 为每层维护 **6 个列表**：新增 `key/value_cache_pack`（uint16 打包数据）与 `key/value_cache_params`（fp32 scale/zero）四组存放低比特主缓存，原 `key_cache`/`value_cache` 复用为 `(b, s, h, d)` 布局的 FP16 残余区。
- `update_residual` 沿 `dim=-3` 追加新 token；`update_pack` 四路拼接中 K 系沿 `dim=-3`、唯 `value_cache_params` 沿 `dim=-1`（其形状 `(b, d/g, h, s)` 把序列放最后以让同组参数内存连续），传 `None` 时兼作主缓存读取器；`clear_residual` 置 `[]` 清空残余。
- 量化不发生在缓存里——容器只管存与拼，量化由 prefill 的 `kvcache_pack_int` 与 decode 残余 kernel 完成。
- 集成策略是"复制模型文件 + 猴子补丁"两板斧：复制 `modeling_llama.py` 并改导入消灭模型侧 import 期绑定；`example.py:9-12` 替换 `transformers.cache_utils` 三个类覆盖包内运行期解析，使官方 `generate` 无感创建改造缓存。
- 这套方案存在边界：残余布局与 `get_seq_length` 的 `shape[-2]` 语义冲突，靠"恒用 flash_attention_2 + generate 提供 cache_position"两个恰好才不触发——换注意力实现即可能出错。

## 7. 下一步学习建议

下一讲 **u6-l2《LlamaBitDecoding 前向：prefill 与 decode 双路径集成》** 将从容器转到使用者：精读 [evaluation/llama.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/llama.py) 中 `LlamaBitDecoding.forward` 的两个分支——prefill 如何先跑 FP16 flash-attn 再切分打包区/残余区，decode 如何补零对齐残余区后交给 `fwd_kvcache_int`。建议提前通读该文件 640–760 行，并留意本讲 4.2.3 标注的每个调用点。之后再进入 u6-l3（Qwen3 集成与后端注册表），总结把 BitDecoding 接入任意 HF 模型的通用清单。
