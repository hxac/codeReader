# u2-l1：Qwen3 模型总体架构

## 1. 本讲目标

从本讲开始，我们正式进入 `reasoning_from_scratch/qwen3.py` —— 全书一切内容的地基文件。第 2~8 章的生成、评测、强化学习、蒸馏，全部建立在这个模型类之上。

学完本讲，你应该能够：

1. 说出 `QWEN_CONFIG_06_B` 配置字典中每个超参数（`emb_dim`、`n_heads`、`n_kv_groups`、`rope_base` 等）的含义和作用。
2. 描述 `Qwen3Model.forward` 的完整数据流：embedding → 28 个 `TransformerBlock` → `final_norm` → `out_head`，并能跟踪每一步的张量形状。
3. 用缩小版配置实例化 `Qwen3Model`，检查输出 logits 的形状，并统计参数量。
4. 解释为什么 0.6B 模型实例化后参数量约为 7.5 亿，而名字却叫 "0.6B"。

本讲只画「地图」：注意力、RoPE、RMSNorm、KV 缓存的内部细节分别由 u2-l2 ~ u2-l5 四讲展开。你在这里只需要知道每个组件「是什么、放在哪、和谁连接」。

## 2. 前置知识

### 2.1 承接前几讲的术语

- **基座模型与推理模型**（u1-l1）：本仓库的主角 `qwen3.py` 定义的是模型*结构*；权重从磁盘加载（u1-l3 讲过的 `download_qwen3_small`）。结构 + 权重 = 一个可运行的模型。
- **token 与 tokenizer**（u1-l2）：模型不认识文字，只认识整数 token id。tokenizer 把文本变成 id 序列，模型把 id 序列变成预测。

### 2.2 本讲新术语

- **logits**：模型最后一层输出的、未经归一化的「打分向量」，长度等于词表大小。对 logits 做 softmax 就得到每个 token 的预测概率。本讲的终点就是拿到 logits。
- **embedding（嵌入）**：一张查找表。`nn.Embedding(vocab_size, emb_dim)` 把每个整数 token id 映射成一个 `emb_dim` 维的稠密向量。这一步没有任何运算，纯粹是「查表」。
- **自回归（autoregressive）生成**：模型一次前向只预测「下一个 token」，把预测拼回输入再前向，循环往复。这就是 u1-l2 中 `generate_simple.py` 逐 token 流式输出的原理。
- **残差连接（residual / shortcut connection）**：把子模块的输入直接加到输出上（`x + f(x)`），让梯度可以「抄近道」流回浅层，是深层网络（28 层！）能训得动的关键。
- **`nn.Module` 与 `nn.ModuleList`**：PyTorch 的模块基类与模块容器。容器里的子模块参数会自动注册给父模块，`model.parameters()` 能一次拿全。
- **bfloat16**：16 位浮点格式，指数位与 float32 相同、尾数更短。数值范围大但精度低，用于把 0.6B 模型的显存占用减半。配置字典中的 `"dtype": torch.bfloat16` 就是让所有层默认用它创建。

### 2.3 一个直觉：配置驱动的模型定义

这个模型文件最「优雅」的一点是：**整个模型由一个 Python 字典完全决定**。想造一个 2 层的小模型？改一个数字。想造 32B 的大模型？换一个字典（附录 C 与 u10-l3 正是这么做的）。理解了「字典 → 结构」的映射，你就掌握了阅读本文件的钥匙。

## 3. 本讲源码地图

| 文件 | 关注范围 | 作用 |
| --- | --- | --- |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | L15-L27、L30-L136 | 本讲主角：配置字典、`Qwen3Model`、`TransformerBlock` |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | L139-L344 | 子组件 `FeedForward`、`GroupedQueryAttention`、RoPE、`RMSNorm`（本讲只点名，细节在后四讲） |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | L658-L661 | 权重绑定分支，用来解释 "0.6B" 命名 |
| [chC/01_main-chapter-code/chC_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chC/01_main-chapter-code/chC_main.ipynb) | 全篇 | 附录 C：从零逐单元格重建同一个模型，最后加载权重生成文本，适合对照阅读 |
| [tests/test_qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py) | L394-L407 | 用「缩小配置」实例化 `Qwen3Model` 的官方范例，本讲实践的依据 |

`qwen3.py` 全文 662 行，按功能分四段：模型结构（L15-L344）、分词器（L347-L419）、下载入口（L440-L533，u1-l3 已讲）、权重加载（L534-L662，附录 C/D 使用）。本讲聚焦第一段的最外两层。

## 4. 核心概念与源码讲解

### 4.1 QWEN_CONFIG_06_B：一个字典定义整个模型

#### 4.1.1 概念说明

`QWEN_CONFIG_06_B` 是一个普通 Python 字典，却完整描述了 Qwen3 0.6B 的骨架：多少层、每层多宽、几个注意力头、位置编码用什么频率。模型类的 `__init__` 只做一件事——从这个字典里取值来创建子模块。

为什么用字典而不是一长串函数参数？因为**同一份模型代码要服务多个尺寸**：本书正文用 0.6B，附录 C 提供 1.7B~32B 的配置（u10-l3），测试用 tiny config（见 4.2.4）。字典可以复制、修改、传递，是「结构」与「尺寸」解耦的最简单手段。

#### 4.1.2 核心流程

12 个键按用途分四组：

| 分组 | 键 | 值 | 作用 |
| --- | --- | --- | --- |
| 词表与上下文 | `vocab_size` | 151,936 | 词表大小，决定 embedding 行数与 logits 长度 |
| | `context_length` | 40,960 | 模型能处理的最大 token 数（训练时的长度），超过即报错 |
| 宽度 | `emb_dim` | 1024 | 每个 token 向量的维度（隐藏层宽度） |
| | `hidden_dim` | 3072 | 前馈网络中间层宽度（emb_dim 的 3 倍，SwiGLU 惯例） |
| | `head_dim` | 128 | 每个注意力头的维度 |
| 深度与头 | `n_heads` | 16 | 查询（query）头数量 |
| | `n_kv_groups` | 8 | KV 组数量（GQA：8 组 K/V 服务 16 个 Q 头） |
| | `n_layers` | 28 | TransformerBlock 层数 |
| | `qk_norm` | True | 是否对 Q、K 做逐头 RMSNorm |
| 位置与其他 | `rope_base` | 1,000,000.0 | RoPE 旋转的频率基座 θ |
| | `dtype` | bfloat16 | 各层权重的创建精度 |

由此可以派生两个值得注意的量：

- 每组共享的 Q 头数：\( n_\text{heads} / n_\text{kv\_groups} = 16/8 = 2 \)。
- 注意力输出宽度：\( n_\text{heads} \times \text{head\_dim} = 16 \times 128 = 2048 \)，**大于** `emb_dim`=1024。这与 GPT-2 时代「头数 × 头维 = 隐藏维」的约定不同——Qwen3 把 `head_dim` 设为独立超参数，允许注意力子层比隐藏层「更宽」。

#### 4.1.3 源码精读

配置字典本体（含作者注释）：

[reasoning_from_scratch/qwen3.py:L15-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L15-L27)

```python
QWEN_CONFIG_06_B = {
    "vocab_size": 151_936,     # Vocabulary size
    "context_length": 40_960,  # Length originally used during training
    "emb_dim": 1024,           # Embedding dimension
    "n_heads": 16,             # Number of attention heads
    "n_layers": 28,            # Number of layers
    "hidden_dim": 3072,        # Size of intermediate dim in FeedForward
    "head_dim": 128,           # Size of the heads in GQA
    "qk_norm": True,           # Whether to normalize queries & keys in GQA
    "n_kv_groups": 8,          # Key-Value groups for GQA
    "rope_base": 1_000_000.0,  # The base in RoPE's "theta"
    "dtype": torch.bfloat16,   # Lower-precision dtype to reduce memory
}
```

这段代码就是 0.6B 模型的「基因」。三个容易忽略的细节：

1. `151_936` 中的下划线是 Python 的数字分隔符，纯粹为了可读性，等价于 151936。
2. `context_length: 40_960` 是 0.6B base 模型训练时的长度。生成时用它预计算整个 RoPE 的 cos/sin 表（见 4.2.3），输入超过它会被 `forward` 直接拒绝。
3. `rope_base` 为 100 万（而非老模型的 1 万），这是长上下文模型（Qwen2.5 起）的常见做法——基座越大，相邻位置的旋转角差异越平缓，长序列上位置信息更不易混淆。细节在 u2-l4。

同一字典在附录 C notebook 中被逐单元格重建并最终使用：

[chC/01_main-chapter-code/chC_main.ipynb:L536](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chC/01_main-chapter-code/chC_main.ipynb#L536) —— notebook 内联定义了 `QWEN_CONFIG_06_B = {`，随后在同一单元格族里逐步实现 RMSNorm、RoPE、GQA 等组件，是「从零搭一遍」的教材版本。

#### 4.1.4 代码实践：探查配置并手算派生量

**实践目标**：把配置字典当作数据来检查，验证 4.1.2 的派生量。

**操作步骤**（在装好本项目依赖的环境里运行，见 u1-l2）：

```python
# explore_config.py —— 示例代码（本讲新写）
import torch
from reasoning_from_scratch.qwen3 import QWEN_CONFIG_06_B as cfg

for k, v in cfg.items():
    print(f"{k:>16}: {v}")

print("group_size (每组共享的 Q 头数) =", cfg["n_heads"] // cfg["n_kv_groups"])
print("注意力输出宽度 d_out          =", cfg["n_heads"] * cfg["head_dim"])
print("是否等于 emb_dim?             =", cfg["n_heads"] * cfg["head_dim"] == cfg["emb_dim"])
print("hidden_dim / emb_dim          =", cfg["hidden_dim"] / cfg["emb_dim"])
```

**需要观察的现象**：`group_size` 为 2；`d_out` 为 2048 且不等于 `emb_dim`；`hidden_dim / emb_dim` 恰为 3.0。

**预期结果**：三行派生值分别为 `2`、`2048`、`False`、`3.0`。（纯 Python 整数运算，无随机性，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：若把 `n_heads` 改为 12 而其余不变，实例化模型会先在哪里报错？

**答案**：在 `GroupedQueryAttention.__init__` 的断言 `num_heads % num_kv_groups == 0`（qwen3.py:158）处——12 不能被 8 整除，`AssertionError: num_heads must be divisible by num_kv_groups`。这说明配置字典本身不校验合法性，约束由使用它的组件执行。

**练习 2**：`head_dim` 设为 `None` 会发生什么？

**答案**：`Qwen3Model.__init__` 里有专门分支（qwen3.py:44-47）：若 `head_dim` 为 `None`，则取 `emb_dim // n_heads`（即 1024/16=64）。这兼容了「不单独指定头维」的老式配置。Qwen3 官方配置显式给出 128。

**练习 3**：为什么 `context_length` 用 40_960 这样「不整」的数？

**答案**：它是 Qwen3 训练时实际使用的序列长度（约 4 万 token），配置注释写明 "Length originally used during training"。模型结构本身不要求它是 2 的幂；它只决定 RoPE 表的大小和输入长度上限。

### 4.2 Qwen3Model：从 token id 到 logits 的主干

#### 4.2.1 概念说明

`Qwen3Model` 是最外层的「容器模块」，只做四件事：

1. **查表**：`tok_emb` 把 token id 变成向量。
2. **过 28 个块**：每个 `TransformerBlock` 做一次注意力 + 前馈（带残差）。
3. **归一化**：`final_norm` 把最终表示稳定到统一尺度。
4. **投影回词表**：`out_head` 把每个位置映射成 `vocab_size` 维打分（logits）。

它还预计算了两样「可复用工具」：RoPE 的 cos/sin 表（注册为 buffer，随模型移动设备但不计入参数）和一个位置计数器 `current_pos`（服务 KV 缓存解码，u2-l5 详讲）。

#### 4.2.2 核心流程

一次前向（不带缓存，即 prefill 路径）的形状流水：

```
输入 in_idx: (b, num_tokens)                整数 token id
   │ tok_emb 查表
   ▼
x: (b, num_tokens, 1024)                    emb_dim
   │ TransformerBlock × 28（形状不变）
   ▼
x: (b, num_tokens, 1024)
   │ final_norm（RMSNorm，形状不变）
   ▼
x: (b, num_tokens, 1024)
   │ out_head 线性投影 1024 → 151936
   ▼
logits: (b, num_tokens, 151936)             每个位置对全词表的打分
```

要点：

- **形状守恒**：28 个块与归一化都不改变 `(b, num_tokens, 1024)`；只有首尾两端改变维度。
- **逐位置并行**：一次前向对序列里*每个*位置都给出 logits。生成时只用最后一个位置的 logits 预测下一个 token；训练/打分时（第 5、8 章）则会用全部位置。
- **因果掩码**：`forward` 内构造上三角布尔掩码，保证位置 \(i\) 只能看见 \( \le i \) 的位置——这是「自回归」的硬保证。

#### 4.2.3 源码精读

**构造函数**——创建四大件并预计算 RoPE 表：

[reasoning_from_scratch/qwen3.py:L30-L56](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L30-L56)

```python
self.tok_emb = nn.Embedding(cfg["vocab_size"], cfg["emb_dim"], dtype=cfg["dtype"])

self.trf_blocks = nn.ModuleList(  # ModuleList since Sequential can only accept one input, and we need `x, mask, cos, sin`
    [TransformerBlock(cfg) for _ in range(cfg["n_layers"])]
)
self.final_norm = RMSNorm(cfg["emb_dim"])
self.out_head = nn.Linear(cfg["emb_dim"], cfg["vocab_size"], bias=False, dtype=cfg["dtype"])
```

这段代码做了四件事：查表嵌入、28 个块的列表、末尾归一化、输出投影。注意那行注释——作者解释了为什么用 `nn.ModuleList` 而非 `nn.Sequential`：每个块需要**四个输入**（`x, mask, cos, sin`），而 `Sequential` 只能传递单个张量。随后的 [L48-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L48-L54) 调 `compute_rope_params` 生成 cos/sin 并用 `register_buffer(..., persistent=False)` 注册：buffer 会随 `.to(device)` 一起移动、但不进入 `state_dict`——所以加载权重的 `.pth` 文件里没有它们，它们完全由配置推导。

**前向函数**——数据流与两处防御：

[reasoning_from_scratch/qwen3.py:L58-L102](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L58-L102)

按段拆解：

1. **查表**（[L60-L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L60-L61)）：`tok_embeds = self.tok_emb(in_idx)`，一步从 id 到向量。
2. **位置与上限检查**（[L63-L70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L63-L70)）：`pos_start` 平时是 0；使用 KV 缓存时取 `self.current_pos`（已生成的 token 数）。若 `pos_end` 超过 `context_length` 立即 `raise ValueError`——这是模型第一道自我保护。
3. **因果掩码**（[L72-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L72-L90)）：`torch.triu(..., diagonal=1)` 生成上三角 `True`（=被禁止注意的位置），无缓存时是 `(num_tokens, num_tokens)`；有缓存时先建满尺寸再切片 `[pos_start:pos_end, :pos_end]`（u2-l5 详讲）。最后 `[None, None, :, :]` 升成四维，让同一个掩码在所有 batch、所有头上广播。
4. **逐块循环**（[L92-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L92-L98)）：

   ```python
   for i, block in enumerate(self.trf_blocks):
       blk_cache = cache.get(i) if cache else None
       x, new_blk_cache = block(x, mask, self.cos, self.sin,
                                start_pos=pos_start,
                                cache=blk_cache)
       if cache is not None:
           cache.update(i, new_blk_cache)
   ```

   28 个块**串行**执行，每块接收同一份 mask 和 cos/sin，各自维护自己的 KV 缓存槽位。x 的形状贯穿不变。
5. **收尾**（[L100-L102](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L100-L102)）：`final_norm` 后 `self.out_head(x.to(self.cfg["dtype"]))`——先显式转回 bfloat16 再投影，保证输出 dtype 与配置一致。

**为什么叫 0.6B 却有 7.5 亿参数**——看权重绑定的证据：

[reasoning_from_scratch/qwen3.py:L658-L661](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L658-L661)

```python
if "lm_head.weight" in params:
    model.out_head.weight = assign(model.out_head.weight, params["lm_head.weight"], "lm_head.weight")
else:
    model.out_head.weight = model.tok_emb.weight
```

加载 Hugging Face 权重时，若 checkpoint 里没有独立的 `lm_head.weight`，就让输出头**直接复用** embedding 权重（权重绑定，weight tying）。Qwen3-0.6B 官方就是绑定的，因此官方口径的参数量不含独立的输出头 ≈ 5.96 亿 ≈ 0.6B。而本仓库 `Qwen3Model` 默认创建独立的 `out_head`，所以全新实例化 `sum(p.numel())` 约 7.5 亿——多出的 1.56 亿正是那张被「重复计算」的输出投影。参数量推算见 5. 综合实践。

**官方用法示范**（加载真实权重并生成）：

[chC/01_main-chapter-code/chC_main.ipynb:L762](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chC/01_main-chapter-code/chC_main.ipynb#L762)

notebook 中的用法是 `model = Qwen3Model(QWEN_CONFIG_06_B)` 后紧跟 `model.load_state_dict(torch.load(model_file))`——结构由本讲的类定义，知识由 `.pth` 权重文件注入。

#### 4.2.4 代码实践：缩小配置实例化模型

**实践目标**：亲手验证「字典 → 结构 → logits 形状」这条链，并把参数量算出来。这是本讲的核心实践。

**操作步骤**：

```python
# tiny_qwen3.py —— 示例代码（本讲新写）
import torch
from reasoning_from_scratch.qwen3 import Qwen3Model, QWEN_CONFIG_06_B

# 1) 复制配置并缩小：只改层数与上下文长度
cfg = dict(QWEN_CONFIG_06_B)          # 浅拷贝即可，我们不嵌套修改
cfg["n_layers"] = 2
cfg["context_length"] = 128
# CPU 上跑不动 bfloat16 的老环境可改用 float32（参考 tests/test_qwen3.py 的 tiny config）
# cfg["dtype"] = torch.float32

# 2) 实例化：随机初始化的权重，无需下载
torch.manual_seed(123)
model = Qwen3Model(cfg)
print("块数:", len(model.trf_blocks))

# 3) 喂入 (1, 10) 随机 token id 并前向
input_ids = torch.randint(0, cfg["vocab_size"], (1, 10))
with torch.inference_mode():
    logits = model(input_ids)

# 4) 断言形状并统计参数量
assert logits.shape == (1, 10, cfg["vocab_size"]), logits.shape
print("logits 形状:", tuple(logits.shape))
print("logits dtype:", logits.dtype)
total = sum(p.numel() for p in model.parameters())
print(f"参数量: {total:,}")
```

**需要观察的现象**：断言通过；`logits.dtype` 为 `torch.bfloat16`（若未改 float32）；打印的参数量约 3.43 亿——虽然只有 2 层，但 embedding + out_head 两张大表就占了约 3.11 亿。

**预期结果**：按 4.3 节的公式推算，2 层模型参数量应为 \( 2 \times 15{,}730{,}944 + 311{,}164{,}928 + 1{,}024 = 342{,}627{,}840 \)。请以本地实际打印为准核对（待本地验证）。

**项目内依据**：测试套件里就有同款「缩小配置」的官方用法——[tests/test_qwen3.py:L394-L407](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L394-L407) 构造了 `vocab_size=257, n_layers=2, emb_dim=32` 的 tiny config 并实例化 `Qwen3Model(cfg)`，用于与 Hugging Face 实现逐 logits 对照。这说明缩小实例化是被项目测试背书的标准操作。

**进一步观察（可选）**：把 `input_ids` 长度改成 200（超过 `context_length=128`），重新前向，应触发 `ValueError: Sequence length ... exceeds the model's context length ...`——亲手验证 4.2.3 第 2 点的防御。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `trf_blocks` 用 `nn.ModuleList` 而不是 `nn.Sequential`？

**答案**：`Sequential` 只能把单个张量从上一个模块传给下一个，而每个 `TransformerBlock.forward` 需要 `x, mask, cos, sin` 四个输入（外加可选 cache），必须手动循环传参。源码注释（qwen3.py:37）原文说明了这一点。`ModuleList` 同样会注册子模块参数，只是不自动执行前向。

**练习 2**：`register_buffer("cos", cos, persistent=False)` 中 `persistent=False` 意味着什么？

**答案**：buffer 会随模型一起 `.to(device)`/`.cuda()` 移动、能被 `model.cos` 访问，但因为 `persistent=False` 不会写进 `state_dict`。这样 `load_state_dict` 加载官方 `.pth` 权重时不会因为多余的键报错——cos/sin 表完全可以由配置重算出来，没必要存盘。

**练习 3**：同一个 (1, 10) 输入跑两次前向（不重建模型、不重置任何状态），两次 logits 会完全一样吗？

**答案**：会一样。不带 `cache` 参数的调用不修改任何权重；唯一的可变状态 `self.current_pos` 只在 `cache is not None` 时才更新（qwen3.py:72-73），纯 prefill 前向是确定性的（dropout 之类在此模型中不存在）。若两次结果不同，那一定是你传了 cache 又没 reset——这正是 u2-l5 的主题。

### 4.3 TransformerBlock：Pre-Norm 残差骨架

#### 4.3.1 概念说明

`TransformerBlock` 是被复制 28 次的基本单元，内部只有两个子模块（注意力 `att`、前馈 `ff`）和两个归一化层（`norm1`、`norm2`）。它的结构是现代 LLM 的标准「**Pre-LayerNorm 残差**」骨架：

\[ x \leftarrow x + \text{Att}(\text{Norm}_1(x)) \qquad x \leftarrow x + \text{FF}(\text{Norm}_2(x)) \]

对比早期 Transformer 的 Post-Norm（先算子模块再归一化），Pre-Norm 把归一化放进残差分支内部、恒等通路上一路畅通，深层网络训练更稳定——这是 Llama、Qwen 系列的共同选择。归一化用 RMSNorm（比 LayerNorm 少减均值、少 bias），前馈用 SwiGLU 三线性层门控，注意力用 GQA——这三者的内部原理分别是 u2-l2、u2-l2、u2-l3 的内容。

#### 4.3.2 核心流程

`TransformerBlock.forward` 的伪代码：

```
输入 x, mask, cos, sin, start_pos, cache

# ── 注意力子层 ──
shortcut = x                     # 保存残差
x = norm1(x)                     # RMSNorm
x, next_cache = att(x, mask, cos, sin, start_pos, cache)
x = x + shortcut                 # 残差相加

# ── 前馈子层 ──
shortcut = x
x = norm2(x)
x = ff(x)
x = x + shortcut

返回 (x, next_cache)
```

注意两个子层共用同一个套路：**归一化 → 变换 → 加残差**。返回值是二元组 `(x, next_cache)`——把 KV 缓存「穿」过整个块传回给 `Qwen3Model` 的循环。

#### 4.3.3 源码精读

[reasoning_from_scratch/qwen3.py:L108-L136](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L108-L136)

```python
class TransformerBlock(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.att = GroupedQueryAttention(
            d_in=cfg["emb_dim"],
            num_heads=cfg["n_heads"],
            head_dim=cfg["head_dim"],
            num_kv_groups=cfg["n_kv_groups"],
            qk_norm=cfg["qk_norm"],
            dtype=cfg["dtype"]
        )
        self.ff = FeedForward(cfg)
        self.norm1 = RMSNorm(cfg["emb_dim"], eps=1e-6)
        self.norm2 = RMSNorm(cfg["emb_dim"], eps=1e-6)

    def forward(self, x, mask, cos, sin, start_pos=0, cache=None):
        shortcut = x
        x = self.norm1(x)
        x, next_cache = self.att(x, mask, cos, sin, start_pos=start_pos, cache=cache)
        x = x + shortcut

        shortcut = x
        x = self.norm2(x)
        x = self.ff(x)
        x = x + shortcut
        return x, next_cache
```

阅读要点：

- 构造函数把配置字典的五个键**就地分发**给 `GroupedQueryAttention`（`emb_dim/n_heads/head_dim/n_kv_groups/qk_norm/dtype`），把整个字典交给 `FeedForward`（它自己取 `emb_dim/hidden_dim/dtype`，见 [L139-L150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L139-L150)）。两种传参风格并存，属作者笔法差异。
- 两个 `RMSNorm` 都显式指定 `eps=1e-6`（而模型级 `final_norm` 用默认值，也是 1e-6，只是写法不同）。
- `forward` 中两次 `x = x + shortcut` 就是 4.3.1 公式的直译；形状自始至终保持 `(b, num_tokens, emb_dim)`。
- `FeedForward` 的三行前向（`silu(fc1(x)) * fc2(x)` 再过 `fc3`）是 SwiGLU 门控，本讲只需知道「输入输出都是 emb_dim 维」；推导在 u2-l2。

#### 4.3.4 代码实践：单独解剖一个块

**实践目标**：把 `TransformerBlock` 从模型中 isolated 出来，验证「形状守恒」并统计它的参数构成。

**操作步骤**：

```python
# inspect_block.py —— 示例代码（本讲新写）
import torch
from reasoning_from_scratch.qwen3 import TransformerBlock, QWEN_CONFIG_06_B

cfg = QWEN_CONFIG_06_B
torch.manual_seed(123)
block = TransformerBlock(cfg)          # 一个真实尺寸（0.6B 配置）的块

x = torch.randn(1, 5, cfg["emb_dim"], dtype=cfg["dtype"])  # dtype 须与权重一致
mask = torch.triu(                      # 复刻 forward 里的因果掩码
    torch.ones(5, 5, dtype=torch.bool), diagonal=1
)[None, None, :, :]

# cos/sin 用零张量占位（RoPE 只做逐元素乘加，不影响形状验证），
# 但行数必须 >= 序列长度 5，因为 apply_rope 会按 [offset:offset+seq_len] 切片
zero_rope = torch.zeros(8, cfg["head_dim"])
out, next_cache = block(x, mask, zero_rope, zero_rope)
print("输入形状:", tuple(x.shape), "输出形状:", tuple(out.shape))

# 参数构成
for name, p in block.named_parameters():
    print(f"{name:>22} {tuple(p.shape)} {p.numel():>10,}")
print("单块参数合计:", f"{sum(p.numel() for p in block.parameters()):,}")
```

**需要观察的现象**：输出形状与输入完全相同 `(1, 5, 1024)`；参数清单按 `att.W_query / att.W_key / att.W_value / att.out_proj / att.q_norm.scale / att.k_norm.scale / ff.fc1 / ff.fc2 / ff.fc3 / norm1.scale / norm2.scale` 逐项列出。

**预期结果**：单块参数合计应为 15,730,944（推算明细见 5. 综合实践的表格；cos/sin 此处传了占位张量，不影响形状验证——RoPE 只做逐元素乘加。待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果把两个 `RMSNorm` 从块里删掉（直接 `att(x)` 再 `ff(x)`），前向还能跑通吗？会损失什么？

**答案**：大概率能跑通——形状上归一化层不改变任何维度。但训练会出问题：残差主干上没有任何尺度约束，28 层累加后数值方差会逐层放大，梯度不稳。Pre-Norm 结构是深层可训练的前提，不能删。

**练习 2**：`TransformerBlock.forward` 为什么返回 `next_cache` 而不是让注意力层自己把缓存存到全局变量？

**答案**：函数式的数据流让缓存的生命周期显式可控：`Qwen3Model.forward` 的循环负责 `cache.get(i)` / `cache.update(i, ...)`（qwen3.py:92-98），每层缓存存在 `KVCache` 对象（qwen3.py:422-437）的对应槽位。这样「生成新的一段」与「读取历史」解耦，`KVCache.reset()` 一句即可清空重来，没有隐藏的全局状态。

**练习 3**：一个 `TransformerBlock` 里有多少个带参数的子模块？分别对应 `load_hf_weights_into_qwen` 里哪些 HF 张量名？

**答案**：11 个带参数子模块，即 `named_parameters()` 的 11 项——att 的 4 个 Linear（W_query/W_key/W_value/out_proj）+ q_norm/k_norm 的 2 个 scale；ff 的 3 个 Linear；norm1/norm2 的 2 个 scale。对应关系：`W_query→q_proj`、`W_key→k_proj`、`W_value→v_proj`、`out_proj→o_proj`、`fc1→gate_proj`、`fc2→up_proj`、`fc3→down_proj`、`norm1→input_layernorm`、`norm2→post_attention_layernorm`，映射代码见 [qwen3.py:L552-L653](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L552-L653)。

## 5. 综合实践：参数量估算器——从配置推算并对照实测

本讲的贯穿任务：写一个函数，**只用配置字典**推算 `Qwen3Model` 的参数量，再与 `sum(p.numel())` 实测对照，最后回答那个反直觉的问题——「0.6B 模型到底有多少参数？」

**任务步骤**：

1. **推导每块的解析公式**。一个 `TransformerBlock` 的参数量：

\[ \begin{aligned}
N_\text{att} &= \underbrace{d \cdot n_h h_d}_{W_q} + \underbrace{d \cdot n_{kv} h_d}_{W_k} + \underbrace{d \cdot n_{kv} h_d}_{W_v} + \underbrace{n_h h_d \cdot d}_{out} + \underbrace{2 h_d}_{qk\text{-}norm} \\
N_\text{ff} &= \underbrace{d \cdot h}_{fc1} + \underbrace{d \cdot h}_{fc2} + \underbrace{h \cdot d}_{fc3} \\
N_\text{norms} &= 2d \\
N_\text{block} &= N_\text{att} + N_\text{ff} + N_\text{norms}
\end{aligned} \]

其中 \( d=\text{emb\_dim},\ h=\text{hidden\_dim},\ n_h=\text{n\_heads},\ n_{kv}=\text{n\_kv\_groups},\ h_d=\text{head\_dim} \)。

2. **实现估算器**（示例代码，本讲新写）：

   ```python
   def estimate_params(cfg, tied=False):
       d, h = cfg["emb_dim"], cfg["hidden_dim"]
       nh, nkv, hd = cfg["n_heads"], cfg["n_kv_groups"], cfg["head_dim"]
       n_blk = (d*nh*hd + 2*d*nkv*hd + nh*hd*d + 2*hd      # 注意力
                + 3*d*h + 2*d)                              # FFN + norms
       total = cfg["n_layers"]*n_blk + cfg["vocab_size"]*d  # 块 + embedding
       if not tied:
           total += cfg["vocab_size"]*d                     # 独立输出头
       total += d                                           # final_norm
       return total
   ```

3. **对照实测**：对 4.2.4 的 2 层缩小配置分别打印 `estimate_params(cfg)` 与 `sum(p.numel())`，两者必须相等。

4. **回放真实尺寸**：代入完整的 `QWEN_CONFIG_06_B`，按下面表格逐项核对推算值：

   | 组件 | 计算 | 参数量 |
   | --- | --- | --- |
   | 单个块 | \( 1024 \times 2048 \times 2 + 1024 \times 1024 \times 2 + 256 + 3 \times 1024 \times 3072 + 2048 \) | 15,730,944 |
   | 28 个块 | \( 28 \times 15{,}730{,}944 \) | 440,466,432 |
   | tok_emb | \( 151936 \times 1024 \) | 155,582,464 |
   | out_head | \( 1024 \times 151936 \) | 155,582,464 |
   | final_norm | 1024 | 1,024 |
   | **合计（独立输出头）** | | **751,632,384 ≈ 7.52 亿** |
   | **绑定输出头（tied）** | 减去 out_head | **596,049,920 ≈ 5.96 亿 ≈ 0.6B** |

5. **得出结论**：Qwen3-0.6B 的官方命名按**绑定输出头**口径计数（HF checkpoint 无独立 `lm_head.weight`，加载时走 [qwen3.py:L658-L661](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L658-L661) 的复用分支）；而本仓库 `Qwen3Model` 全新实例化时 `out_head` 是独立参数，所以实测约 7.5 亿。两个数字都对，口径不同。

6. **（可选）观察缩放规律**：把 `n_layers` 从 2 扫到 28，画出参数量随层数的曲线——完全线性，斜率即单块参数量。这正是「同一个类服务 0.6B~32B」的含义。

**预期结果**：第 3 步两个数字逐位相等；第 4 步表格与实测一致（上表为解析推算值，具体数字待本地验证）。

## 6. 本讲小结

- `QWEN_CONFIG_06_B` 用 12 个键完整描述 0.6B 模型骨架：宽度（`emb_dim=1024`、`hidden_dim=3072`）、深度（`n_layers=28`）、头（`n_heads=16`、`n_kv_groups=8`、`head_dim=128`）、位置（`rope_base=1e6`、`context_length=40960`）与精度（`bfloat16`）。
- `Qwen3Model.forward` 数据流：`tok_emb` 查表 → 构造因果掩码 → 28 个 `TransformerBlock` 串行 → `final_norm` → `out_head` 投影到 151,936 维 logits；块内形状守恒为 `(b, num_tokens, 1024)`。
- `trf_blocks` 用 `nn.ModuleList` 而非 `Sequential`，因为每块需要 `x, mask, cos, sin` 四个输入，必须手写循环传参。
- `TransformerBlock` 是 Pre-Norm 残差骨架：`x + Att(Norm1(x))` 与 `x + FF(Norm2(x))`，恒等通路保证 28 层可训练。
- cos/sin 表以 `persistent=False` 的 buffer 注册：随模型移动设备但不进 `state_dict`，由配置随时重算。
- 「0.6B」是绑定输出头的口径（约 5.96 亿参数）；本仓库独立 `out_head` 的全新实例约 7.52 亿参数——参数量口径差异来自 [qwen3.py:L658-L661](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L658-L661) 的权重绑定分支。

## 7. 下一步学习建议

本讲搭好了骨架，块内部的三个组件是接下来四讲的内容，建议按依赖顺序学习：

1. **u2-l2（RMSNorm 与 SwiGLU）**：先攻两个「小而独立」的组件——`RMSNorm` 的 fp32 上抛技巧与 `FeedForward` 的门控结构，它们不依赖其他组件，最适合先学。
2. **u2-l3（GQA 注意力）**：本块最核心的子模块，Q/K/V 投影、头维 reshape、QK-Norm、`repeat_interleave` 展开与缩放点积注意力。
3. **u2-l4（RoPE）**：位置编码的旋转数学，理解 `forward` 里 `cos/sin` 与 `start_pos` 参数的用途。
4. **u2-l5（KV Cache 与因果掩码）**：把本讲刻意略过的 `cache` 参数与 `current_pos` 讲透，prefill 与解码两阶段的掩码切片就水落石出了。

配套阅读：附录 C notebook（[chC/01_main-chapter-code/chC_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chC/01_main-chapter-code/chC_main.ipynb)）从零逐单元格重建了本讲的全部组件，适合在学完 u2-l2~u2-l5 后通读一遍做总复习；学有余力可提前看 `tests/test_qwen3.py` 中 `test_qwen3_base_equivalence_with_transformers`（[L389](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L389) 起），它用 tiny config 证明本实现与 Hugging Face 官方实现逐 logits 等价。
