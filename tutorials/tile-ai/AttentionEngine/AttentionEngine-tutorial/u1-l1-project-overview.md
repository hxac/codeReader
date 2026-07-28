# AttentionEngine 是什么：可定制注意力的代码生成框架

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一句话说清 AttentionEngine 要解决的问题——为什么自定义注意力难写又难优化。
2. 理解它的核心思路：**前端用 Python 函数描述注意力 → 后端自动生成高性能 GPU kernel**，也就是「编译式注意力」。
3. 区分它支持的**两条计算路线**：transformer 注意力（`score_mod` + `online_func`）与线性注意力（`q_mod` / `k_mod` / `v_mod` / `decay_mod`）。
4. 知道它有两种**代码生成后端**：TileLang（`tl`）与 CuTe（`cute`），以及它们各自的产物形态。

本讲是整个学习手册的第一篇，重在建立全局认识，**不要求你马上读懂每一行源码**。后续讲义会沿「用户 API → 符号 IR → 降级 → 模板 → 引擎」这条链逐层深入。

---

## 2. 前置知识

本讲面向零基础读者，但有几类背景知识会让理解更顺畅：

- **注意力机制**：Transformer 的核心运算是「查询 Q、键 K、价值 V」。最朴素的 softmax 注意力是 \(o=\mathrm{softmax}(q k^\top/\sqrt{d})\,v\)。如果你只会这一种也没关系，本系列会逐步带你看懂它的各种变体。
- **kernel / 算子**：在 GPU 上跑的一段底层计算程序叫一个 kernel（算子）。把「缩放 + mask + softmax + 矩阵乘」等多个小步骤合并进同一个 kernel，称为 **fused kernel**（融合算子），可以大幅减少访存、提升性能。FlashAttention 就是最著名的 fused softmax 注意力 kernel。
- **IR（中间表示）与编译**：如果你听过编译器的「源码 → 中间表示 → 目标代码」流程，就能类比 AttentionEngine：用户的 Python 函数先被翻译成一种符号中间表示，再被翻译成 TileLang / CuTe 这种目标代码。
- **PyTorch 的前向 / 反向**：本讲的示例最终都会得到一个可以像 `out = mod(q, k, v)`、`out.backward()` 这样调用的 PyTorch 模块。

不懂上面任何一条也不影响你读完本讲，我们会在用到时随时解释术语。

---

## 3. 本讲源码地图

本讲只读「项目入口文档」和「最外层接口」，目的是建立鸟瞰图，不进入实现细节。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md) | 项目定位、安装方式、Quick Start 全量示例、Roadmap。本讲最重要的入口。 |
| [docs/API.md](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md) | 用户层 API 规范：transformer 注意力与线性注意力两套接口的签名。 |
| [attn_script/Readme.md](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md) | 前端说明，用伪代码解释了「注意力到底在算什么」以及 online 算法模板。 |
| [attn_script/mha.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py) | 标准因果 softmax 注意力的完整用户代码（约 120 行），是贯穿全册的「参照样本」。 |
| [attention_engine/attn_engine/attn_engine.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py) | `AttentionEngine` 引擎入口：选择后端、按形状分发到不同降级函数、编译并缓存 kernel。 |
| [attention_engine/attn_engine/linear_attn_engine.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/linear_attn_engine.py) | `LinearAttentionEngine`：线性注意力的对应入口。 |

> 提示：本讲引用源码时，链接形如 `文件路径#L起始-L结束`，会指向当前 HEAD（`b7088e28`）下的精确行号。后续讲义再带你看 `core/` 下的 IR、降级与模板实现。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目定位与动机**、**编译式注意力框架理念**、**tl 与 cute 两种后端**。

### 4.1 项目定位与动机

#### 4.1.1 概念说明

在 AttentionEngine 出现之前，要实现一个「自定义注意力」通常只有两条路，且二者不可兼得：

1. **灵活路线**：用 PyTorch 张量运算直接写，例如 `scores = q @ k; p = scores.softmax(-1); o = p @ v`。优点是好写、好改；缺点是中间结果（`scores`、`p`）都要落到显存，访存巨大，**性能远低于 FlashAttention 这类 fused kernel**。
2. **高性能路线**：手写 TileLang / CuTe / CUDA 的 fused kernel。优点是性能可逼近 FlashAttention；缺点是开发门槛极高，每种注意力变体都要重写一遍，**灵活性极差**。

AttentionEngine 要解决的就是这个矛盾——**让自定义注意力既灵活又高性能**。它的 README 第一句就点明了定位：

> AttentionEngine is a unified framework to customize attention... provides users with pythonic interface to define customized attention **flexibly** and automatically generate device code with **high performance**.

并给出了一个标志性数字：用户**只需约 80 行 Python 代码**定义 softmax 注意力，就能**自动得到优化好的 fused kernel**。

#### 4.1.2 核心流程

从用户视角看，使用流程只有三步：

1. **描述注意力**：用几个 Python 函数（`score_mod` / `mask_mod` / `online_func` 等）写出你想要的注意力逻辑。
2. **构造引擎**：把形状元信息 `qkv_meta` 和上面的函数一起传给 `AttentionEngine(...)`，引擎在这一步完成「编译」，生成并加载 kernel。
3. **像普通 PyTorch 模块一样调用**：`out = mod(q, k, v)`、`out.backward(do)`。

换句话说，注意力逻辑写在「步骤 1」，而「步骤 2」之后用户几乎感觉不到自己写过 GPU 代码。

#### 4.1.3 源码精读

README 的项目定义与「80 行」承诺在：

- [README.md:1-3](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L1-L3) — 点明「unified framework」「pythonic interface」「high performance」三个关键词，并给出「80 行代码自动得到 fused kernel」的核心卖点。
- [attn_script/Readme.md:1-3](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md#L1-L3) — 进一步把 AttentionEngine 定位为「前端（frontend）」，并说它生成的 kernel **性能可与 FlashAttention 媲美**（comparable to flashAttention）。

`attn_script/mha.py` 就是这「约 80 行」的真实样本——它包含一个 `score_mod`、一个 `causal_mask`、一个 `OnlineSoftmax` 类，再加上主流程里的形状定义和 `AttentionEngine(...)` 构造，整份文件约 120 行（其中真正描述注意力逻辑的核心不到 80 行）。我们会在第 4.2 节拆解它。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 README 的核心承诺，并建立「逻辑描述 vs 生成产物」的直觉。

**操作步骤**：

1. 打开 [README.md:1-3](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L1-L3)，把其中描述「灵活」与「高性能」的两个英文词抄下来。
2. 打开 [attn_script/mha.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py)，数一数：从文件开头到 `mod = AttentionEngine(...)` 构造完成，一共有多少行是「描述注意力逻辑」（即 `score_mod`、`causal_mask`、`OnlineSoftmax` 类这些）。本系列后续会称这几十行为「用户的逻辑描述」。
3. 暂时**不必运行**（运行环境会在下一讲 `u1-l2` 搭建）。如果你现在就想跑，需要按 README 配好 TileLang 与 CUDA 环境；具体步骤见下一讲。

**需要观察的现象**：你会发现，这份文件里**没有任何一行 TileLang / CUDA 代码**——`score_mod` 只是 `return score * softmax_scale`，`OnlineSoftmax` 只是用一些符号运算描述了在线 softmax 的递推。但它最终会变成一个高性能 fused kernel。

**预期结果**：能复述「用户写的是纯 Python 的逻辑描述，GPU kernel 由框架自动生成」这一句话。

> 待本地验证：本讲不要求运行；若你已配好环境，可顺手确认 `mha.py` 能 `import attn_engine` 成功（真正跑通在 `u1-l2`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能「既要灵活又要高性能」，传统做法只能二选一？

> **参考答案**：灵活的写法（PyTorch 张量运算）会把中间矩阵 `scores`、`p` 物化到显存，访存开销大；高性能写法（手写 fused kernel）把所有步骤融合在一个 kernel 里，省掉中间访存，但要为每种注意力变体重写底层代码，维护成本极高。AttentionEngine 的价值就是用「代码生成」让两者兼得。

**练习 2**：README 里「80 行代码」指的到底是哪一部分代码？

> **参考答案**：指**用户为了描述自己的注意力逻辑而写的 Python**（如 `mha.py` 里的 `score_mod`、`mask_mod`、`OnlineSoftmax` 类与主流程的形状/引擎构造），而不包括框架内部自动生成的 TileLang / CuTe kernel 代码。

---

### 4.2 编译式注意力框架理念

#### 4.2.1 概念说明

AttentionEngine 本质是一个**编译器**：它把「注意力的人类描述」翻译成「GPU 设备代码」。理解了这一点，后面所有的源码模块（符号 IR、降级、模板、引擎）就都只是这条编译链上的一个阶段。

用户需要描述的东西，被官方 API 分成几个清晰的组件。对应到 transformer 注意力路线，主要有四件：

| 组件 | 含义 | 直觉 |
| --- | --- | --- |
| `score_mod` | 对注意力分数（scores）做**逐元素**变换，例如乘以缩放系数、加偏置 | 「我想把分数再加工一下」 |
| `mask_mod` | 根据下标 `(b, h, q_idx, kv_idx)` 返回布尔值，决定哪些位置被遮蔽 | 「因果遮罩 / 滑动窗口等」 |
| `online_func` | 一个描述**行级在线算法**的类（如 online softmax），决定分数如何聚合成权重 | 「softmax / sigmoid / retention 等的非线性」 |
| `custom_fwd_inputs` | 用户额外传入的自定义张量（如可学习偏置） | 「我还想喂一些额外输入」 |

这套 API 的规范在 [docs/API.md:4-19](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L4-L19)。

#### 4.2.2 核心流程

`attn_script/Readme.md` 用伪代码说清了 transformer 注意力到底在算什么：

```
scores = query_mod(query) @ key_mod(key)
scores = block_mask(scores)
scores = score_mod(scores)
p      = online_func(scores)     # 行级在线算法
o      = p @ value_mod(value)
```

这个循环对 KV 序列分块进行，是「online（在线）」算法——它不需要把整个 \(S \times S\) 的 scores 矩阵物化出来，而是**逐块更新**。这正是 FlashAttention 省显存的关键思想。其在线前向模板可写成：

\[

o \leftarrow 0;\quad

\text{for each KV block:}\quad
\text{scores}=q@k;\;

\text{scores, online\_rowscales, o\_scale}=\text{online\_fwd}(\text{scores});\quad

o = o \cdot \text{o\_scale} + \text{scores} @ v

\]

最后用 `online_fwd_epilogue` 收尾（例如 softmax 的「除以总和」）。

> 这里的 `online_rowscales`（如 softmax 里的最大值 `m`、分母累加 `r`）和 `final_rowscales`（如 `lse`）就是 online 算法在分块之间需要携带的「状态」。这部分会在 `u2-l6`（OnlineFunc 降级）详讲。

**两条计算路线**。AttentionEngine 把自定义注意力分成两族：

- **Transformer 注意力**（稠密矩阵乘 + 在线归一化）：softmax / sigmoid / relu / retention 等都走 `AttentionEngine`，对应 `score_mod` + `online_func`。
- **线性注意力**（递推 / 分块的状态空间式计算）：mamba2 / simple_gla / retnet_linear 等走 `LinearAttentionEngine`，对应 `q_mod` / `k_mod` / `v_mod` / `decay_mod`，见 [docs/API.md:81-107](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L81-L107)。

两者的区别会在 `u4-l1`（线性注意力引擎）深入，本讲只需知道「入口不同、API 不同」即可。

#### 4.2.3 源码精读

**① transformer 注意力的标准样本**——`mha.py`：

- `score_mod` 只做缩放：[attn_script/mha.py:23-25](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L23-L25) 说明分数如何被逐元素加工。
- `causal_mask`：[attn_script/mha.py:17-18](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L17-L18) 说明 `q_idx >= kv_idx` 即因果遮罩。
- `OnlineSoftmax.online_fwd`：[attn_script/mha.py:47-63](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L47-L63) 是 online softmax 的分块递推（更新 `m`、`r`，并产出 `o_scale` 用于重缩放 `o`）。
- 构造引擎：[attn_script/mha.py:109-117](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L109-L117) 把上述组件交给 `AttentionEngine`，注意 `tune` / `tune_file` / `infer_mask` 等编译期开关。

**② 用户 API 规范**——`docs/API.md`：

- transformer 注意力签名：[docs/API.md:12-19](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L12-L19)。
- 线性注意力签名（另一条路线）：[docs/API.md:90-98](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L90-L98)。

**③ 编译后的调用方式**——用户拿到 `mod` 后，调用方式与普通 PyTorch 一致：[docs/API.md:21-30](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L21-L30) 说明 `output = mod(q, k, v, ...)` 与 `output.backward(do)`。

**④ 在线算法的通用模板**——[attn_script/Readme.md:45-65](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md#L45-L65) 给出 online forward 的分块伪代码，帮助你把 `mha.py` 里的 `online_fwd` 对应到「它在 kernel 里到底被怎么用」。

#### 4.2.4 代码实践

**实践目标**：把「用户的逻辑描述」与「伪代码模板」对上号，确认你读懂了 transformer 注意力的描述方式。

**操作步骤**：

1. 打开 [attn_script/Readme.md:19-31](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md#L19-L31) 的伪代码。
2. 打开 [attn_script/mha.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py)，做一张对照表：伪代码里的 `query_mod / block_mask / score_mod / online_func / value_mod`，分别对应（或不对应）`mha.py` 里的哪段代码。
3. 注意 `mha.py` 里**没有**显式的 `query_mod` / `value_mod`——这说明它们默认是「恒等函数」（identity），对应 [attn_script/Readme.md:36](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md#L36) 的说明。

**需要观察的现象**：你会看到用户只写了 `score_mod`（缩放）、`mask_mod`（因果遮罩）、`online_func`（online softmax）三样，其余步骤由框架补全。

**预期结果**：能填出类似下表的对应关系（待本地验证，纯阅读即可完成）：

| 伪代码步骤 | mha.py 中的对应 |
| --- | --- |
| `query_mod(query) @ key_mod(key)` | 默认恒等，故即 `q @ k` |
| `block_mask(scores)` | 由 `causal_mask` 推导出的块级 mask |
| `score_mod(scores)` | `score * softmax_scale` |
| `online_func(scores)` | `OnlineSoftmax` 的 `online_fwd` / `online_fwd_epilogue` |
| `value_mod(value)` | 默认恒等 |

#### 4.2.5 小练习与答案

**练习 1**：`score_mod` 与 `online_func` 的职责有什么本质区别？

> **参考答案**：`score_mod` 是**逐元素**变换（对每个 `scores[b,h,i,j]` 独立操作，如乘缩放、加偏置）；`online_func` 是**行级在线算法**（需要沿 kv 维做 `reduce_sum` / `reduce_max` 并在分块间维护状态，如 softmax 的 `m`、`r`）。前者不带跨元素归约，后者带。

**练习 2**：为什么 `online_func` 要拆成 `online_fwd` 和 `online_fwd_epilogue` 两段？

> **参考答案**：因为在线算法是分块递推的——`online_fwd` 在每个 KV 块上更新状态并产出 `o_scale` 重缩放累积量 `o`；等所有块处理完，再用 `online_fwd_epilogue` 做收尾（例如 softmax 最后「除以归一化常数」）。拆成两段正好对应「循环体」与「循环后收尾」。

---

### 4.3 tl 与 cute 两种后端

#### 4.3.1 概念说明

「编译式」框架的一个特点是：**同一份用户描述，可以翻译到不同的目标语言**。AttentionEngine 目前有两种后端：

- **`tl`（TileLang）后端**：把用户描述编译成 **TileLang Python 代码**（用 `T.prim_func` 描述的 kernel），TileLang 再进一步编译成 GPU 可执行码。这是**默认后端**。
- **`cute`（CuTe）后端**：把用户描述编译成 **CuTe C++ 代码**（`.h` / `.cu`，类似 FlashAttention 的 Hopper 实现），再编译成可被 Python 调用的 `flash_attn_interface.py`。

两者的**输入相同**（都是 `score_mod` / `mask_mod` / `online_func`），**输出形态不同**（一个是 TileLang Python，一个是 C++）。所以你切换后端时，**用户侧的注意力描述代码基本不用改**。

> 名词解释：**TileLang** 是 tile-ai 社区的 GPU kernel DSL（用 Python 写 kernel，带调度原语）；**CuTe** 是 NVIDIA Cutlass 的张量模板库，常用于手写 Hopper（H100）上的高性能 GEMM/Attention。两者都是「写 GPU kernel 的方式」，只是抽象层级和语言不同。

#### 4.3.2 核心流程

在 `AttentionEngine.__init__` 里，第一步就是根据 `backend` 参数分流：

```
if backend == "tl":   _compile_tl(...)        # 生成 TileLang 代码
elif backend == "cute": lower_cute(...)        # 生成 CuTe C++ 代码
```

无论走哪条，最终都要落到三件事：

1. **生成目标代码**（TileLang 字符串或 C++ 文件）。
2. **编译并落盘**：用一个 `code_hash`（md5）做缓存键，把生成代码写到 cache 目录，命中则跳过重新生成。
3. **动态加载**：用 `importlib` 把生成的模块加载进来，把其中的 `attention`（或 `flash_attn_func`）函数挂到 `self.attention` 上，使 `mod(q,k,v)` 能直接调用。

对 `tl` 后端，还会进一步根据**输入形状**决定走哪一个降级函数（普通训练 / GQA / decode / MLA decode 等），这一「形状分发」逻辑是 `u3-l3` 的主题。

#### 4.3.3 源码精读

**① 后端分支**——[attention_engine/attn_engine/attn_engine.py:122-137](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L122-L137) 是 `backend == "tl"` 分支（调用 `_compile_tl`）；[attention_engine/attn_engine/attn_engine.py:138-216](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L138-L216) 是 `backend == "cute"` 分支（调用 `lower_cute`，并决定输出 `flash_attn_interface.py` 还是 `flash_mla_interface.py`）。默认 `backend="tl"` 见构造函数签名 [attention_engine/attn_engine/attn_engine.py:109-115](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L109-L115)。

**② 形状分发（仅 tl 后端）**——`_select_lower_template` 根据查询序列长 / 头数关系，分发到不同降级函数：[attention_engine/attn_engine/attn_engine.py:218-332](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L218-L332)。其中典型的「训练前向+反向」路径见 [attention_engine/attn_engine/attn_engine.py:292-313](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L292-L313)（`q_seqlen == kv_len and head == head_kv` 时调用 `core/lower/lower.py` 的 `lower_tl`）。

**③ 编译 + 缓存 + 动态加载**——`_compile_tl` 用 md5 做缓存键、写文件、`importlib` 加载：[attention_engine/attn_engine/attn_engine.py:369-382](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L369-L382)。

**④ 调用入口**——`__call__` 把请求转发给加载好的 kernel：[attention_engine/attn_engine/attn_engine.py:388-395](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L388-L395)。

**⑤ 支持的设备**——README 列出已验证设备：[README.md:5-9](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L5-L9)（NVIDIA H100 已验证；AMD MI250 为 TODO）。这与「两种后端」相关：`cute` 后端目前主要面向 NVIDIA Hopper。

**⑥ 现阶段的限制**——Roadmap 标注了 `cute` 后端**暂不支持反向**：[README.md:166-170](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L166-L170)。

#### 4.3.4 代码实践

**实践目标**：在源码里亲眼看到「同一入口，两种后端」与「md5 缓存」这两件事。

**操作步骤**：

1. 打开 [attention_engine/attn_engine/attn_engine.py:122-137](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L122-L137)，确认 `backend == "tl"` 时调用的是 `self._compile_tl(...)`。
2. 紧接着看 [attention_engine/attn_engine/attn_engine.py:138-216](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L138-L216)，确认 `backend == "cute"` 时调用的是 `from core.lower.lower_cute import lower_cute`。
3. 看 [attention_engine/attn_engine/attn_engine.py:369-382](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L369-L382)，找到 `hashlib.md5(tl_code.encode())` 与 `importlib.util.spec_from_file_location`，确认「代码内容 → md5 → cache 文件名 → 动态加载」这条链。
4. 思考：如果同一份注意力描述，你改了 `score_mod` 里一个系数，`code_hash` 会不会变？生成的 `.py` 会不会重新写？

**需要观察的现象**：`_compile_tl` 中 `if not os.path.exists(file_path)` 才写文件——也就是说，只要生成的代码字符串没变（md5 不变），就**直接复用** cache 里的旧模块，跳过重新生成。

**预期结果**：能口头复述「`backend` 参数决定目标语言；无论哪种后端，最终都通过 md5 缓存 + importlib 加载，挂到 `self.attention`」。

> 待本地验证：跑通后（见 `u1-l2`），可以在 `attention_engine/attn_engine/cache/` 目录下看到以 md5 命名的 `.py` 文件，这就是 TileLang 后端的生成产物。

#### 4.3.5 小练习与答案

**练习 1**：`tl` 与 `cute` 两种后端的「生成产物」分别是什么？

> **参考答案**：`tl` 后端生成 TileLang 的 Python kernel 代码（`T.prim_func`，最终被 cache 成 `{md5}.py`，并暴露 `attention` 函数）；`cute` 后端生成 CuTe 的 C++/头文件（`.h`/`.cu`）并渲染出 `flash_attn_interface.py`（或 MLA 的 `flash_mla_interface.py`），最终暴露 `flash_attn_func` / `flash_mla_with_kvcache`。

**练习 2**：为什么用 `code_hash = md5(tl_code)` 做缓存键，而不是用「文件名 + 时间」？

> **参考答案**：因为缓存想表达的是「生成代码是否完全相同」。md5 对代码字符串求哈希：只要用户描述与形状没变，生成代码就一字不差，md5 命中、直接复用；一旦改了 `score_mod` 等逻辑，代码字符串变化，md5 变化，自然会走「重新生成 + 写新文件」的分支。这比按时间或名字更准确。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**阅读 + 归纳型**任务（无需运行环境，纯阅读源码即可完成）。

**任务**：

1. 阅读 [README.md:11-22](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L11-L22)，列出 AttentionEngine 在 `attn_script/` 下给出的全部自定义注意力示例，并按「transformer 注意力 / 线性注意力」分成两类。预期答案应包含：softmax（`mha.py`）、sigmoid（`sigmoidattn.py`）、relu（`reluattn.py`）、retention（`retention.py`）；以及 mamba2（`mamba2_ngroup1.py`）、simple_gla（`simple_gla.py`）、retnet_linear（`retnetion_linear.py`）。
2. 写一段话（5–8 句）描述 AttentionEngine 是如何「把约 80 行 Python 定义自动变成优化 fused kernel」的，要求覆盖以下要点：
   - 用户用 `score_mod` / `mask_mod` / `online_func`（或线性注意力的 `q_mod` / `k_mod` / `v_mod` / `decay_mod`）描述注意力逻辑；
   - 这些描述被框架当作「符号 IR」处理（具体机制在 u2 讲义展开）；
   - 引擎按 `backend` 选择 `tl` 或 `cute` 后端，生成 TileLang / CuTe 代码；
   - 生成代码经 md5 缓存、`importlib` 动态加载，最终暴露成可 `mod(q,k,v)` 调用、可 `.backward()` 的模块。
3. 把你的描述与 [README.md:1-3](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L1-L3) 的官方一句话定义对照，检查自己有没有遗漏「flexibly / high performance / fused kernel」这几个关键词。

**预期结果**：产出一张「示例分类表」+ 一段覆盖四个要点的中文说明。这是后续讲义的认知基础——后续每一篇讲义，本质都是在展开本任务第 2 步里那几个分号之间的某一段。

> 待本地验证：任务本身为纯阅读，无需运行；若想顺便验证示例可运行，请在 `u1-l2` 搭好环境后再做。

---

## 6. 本讲小结

- AttentionEngine 解决「自定义注意力**既灵活又高性能**」的矛盾：用户写 Python 描述，框架自动生成 fused kernel，性能对标 FlashAttention。
- 它是一个**编译式注意力框架**：前端用 `score_mod` / `mask_mod` / `online_func` / `custom_fwd_inputs` 描述注意力，后端把它翻译成 GPU 设备代码。
- 它支持**两条计算路线**：transformer 注意力（`AttentionEngine` + 在线算法）与线性注意力（`LinearAttentionEngine` + q/k/v/decay_mod）。
- 它有**两种生成后端**：`tl`（TileLang Python，默认）与 `cute`（CuTe C++，面向 Hopper），二者输入相同、产物形态不同。
- 引擎统一通过 **md5 缓存 + importlib 动态加载**把生成代码挂成可调用的 `self.attention`，并按输入形状在 `tl` 后端内进一步分发降级函数。
- 目前能力边界见 Roadmap：CuTe 后端反向、AMD MI250、更多稀疏 mask 等仍在推进中。

---

## 7. 下一步学习建议

本讲建立了全局认识，但还没真正跑过任何东西，也没看过内部实现。建议按以下顺序继续：

1. **下一讲 `u1-l2`：环境搭建与运行第一个 softmax attention**。跟着 README 配好 PYTHONPATH / TileLang，亲手跑通 `mha.py`，看到真实的 latency / tflops，并理解 `meta_tensor` 形状元信息的作用。
2. 之后 `u1-l3`（目录结构与代码地图）会带你建立「找代码」的心智地图，理清 `core/` 下 transform / codegen / lower / template 四层关系。
3. 再之后 `u1-l4`（用户 API 全景）会逐一拆解 `score_mod` / `mask_mod` / `online_func` / `CustomIO` 的签名与组合方式。
4. 进入第二单元（`u2`）后，你才会真正打开 `core/transform/graph.py` 与 `core.py`，看到「符号 IR」的内部结构——也就是本讲反复提到、但刻意留到后面的那一段编译链核心。

如果你想提前翻一翻源码找感觉，可以从本讲「源码地图」里的 `attn_script/mha.py` 入手：把它和 `attn_script/Readme.md` 的伪代码对照阅读，是理解整个项目最快的方式。
