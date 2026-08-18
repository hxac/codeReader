# 性能工程：torch.compile、flex_attention 与数据预取

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `torch.compile(model, dynamic=True)` 的工作方式，说清「符号形状」如何减少重编译次数，以及 `recompile_limit` 被提到 64 的原因。
2. 说明 `flex_attention` + `BlockMask` 相对稠密注意力掩码在显存与计算量上的收益，并指出代码里两条不同的编译封装路线（DSpark 整体编译 vs Eagle3 局部编译单例）。
3. 逐行讲解 `CUDAPrefetcher` 如何用后台线程 + 独立 CUDA stream 让数据加载、H2D 拷贝与计算三段重叠，以及 `no_sync` 如何把 G 次梯度规约合并成 1 次。
4. 比较 FSDP 四种 `sharding_strategy` 的显存/通信取舍，理解 DeepSpec 为什么默认 `no_shard`。
5. 独立设计并执行一个性能消融实验：分别关闭 torch.compile、把 flex_attention 换成稠密掩码 + eager、去掉 CUDAPrefetcher，各跑固定步数并记录每步耗时与显存峰值。

本讲是「读代码」转向「调代码」的一讲：前面六个单元我们搞清楚了 DeepSpec 在做什么，这一讲专门回答「它为什么跑得快、哪些开关控制速度、如何用实验量化每个开关的贡献」。

## 2. 前置知识

### 2.1 torch.compile 的两个阶段

`torch.compile` 是 PyTorch 2.x 的编译入口，背后是两级流水：

- **Dynamo（图捕获）**：用字节码拦截把 Python 前向代码翻译成一张 FX 计算图。捕获时会检查输入的形状、dtype、设备等「事实」，这些事实叫 **guard**。
- **Inductor（代码生成）**：把计算图编译成 Triton/fused kernel，减少 kernel 启动次数、融合访存密集算子。

关键行为：每次调用时先评估 guard，guard 全部成立就直接用已编译版本；任何一个不成立（比如 `seq_len` 从 4096 变成 4031），就**重编译**一次。重编译次数达到上限 `recompile_limit` 后，Dynamo 放弃编译、回退到普通 eager 执行。

`dynamic=True` 的作用是让 Dynamo 把变化的维度（如 `seq_len`）当成**符号量**而不是具体数字，于是「seq_len=4096」和「seq_len=4031」共用同一份编译产物——这正是 DeepSpec 训练变长样本时需要的（u2-l6 讲过，缓存样本变长、collator 只做右侧零填充，每个 batch 的真实长度都在变）。

### 2.2 CUDA stream：GPU 上的「多车道」

一个 CUDA stream 是一串按序执行的 GPU 任务队列；不同 stream 之间可以并行。把 H2D（host-to-device）拷贝放进独立 stream，计算 kernel 就不必等拷贝结束。配套的两个原语：

- `wait_stream`：让当前 stream 等待另一个 stream 上的任务完成（建立依赖）。
- `record_stream`：告诉缓存分配器「这块内存另一个 stream 还在用」，防止显存被提前回收复用。

### 2.3 FSDP 与 no_sync（回顾）

u3-l1/u3-l2 已建立：FSDP 包装模型做数据并行；`model.no_sync()` 上下文内的 backward 只累积本卡梯度、不触发通信，把每个优化器步内 G 个微批的 G 次梯度规约合并为 1 次。本讲我们从通信量和显存的角度量化它。

### 2.4 本讲与依赖讲义的衔接

- **u3-l2**：主循环里 `no_sync`、`CUDAPrefetcher` 的位置已经出现过，本讲展开它们各自省下的时间。
- **u4-l1**：DSpark 的非因果掩码「上下文 ∪ 本块内、块间隔离」已作为构图规则讲过，本讲讲它在工程上为什么必须用 `BlockMask` 而不是稠密掩码来承载。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| `deepspec/trainer/base_trainer.py` | torch.compile 的调用点、FSDP 包装与 sharding_strategy 映射、DataLoader 构建、主循环里 CUDAPrefetcher 与 no_sync 的配合 |
| `deepspec/data/cuda_prefetcher.py` | 双缓冲预取器全部实现（74 行，本讲逐行精读） |
| `deepspec/modeling/eagle3/common.py` | Eagle3 的编译封装：recompile_limit 提升、flex_attention/create_block_mask 的模块级编译单例、掩码构造 |
| `deepspec/modeling/dspark/common.py` | DSpark 的 `create_dspark_attention_mask`：mask_mod → BlockMask |
| `deepspec/modeling/dspark/qwen3/modeling.py` | DSpark 注意力层如何按 `_attn_implementation` 分发（含 GQA 展开与 eager 回退） |
| `deepspec/modeling/eagle3/qwen3/modeling.py` | Eagle3 注意力层的 flex 直调与稠密掩码回退分支 |
| `config/dspark/dspark_qwen3_4b.py` 等配置 | `torch_compile`、`sharding_strategy` 两个性能开关的实际取值 |

## 4. 核心概念与源码讲解

本讲四个最小模块：4.1 torch.compile 与动态形状；4.2 flex_attention block mask；4.3 CUDA stream 预取与 no_sync；4.4 FSDP sharding_strategy。前三者是规格指定的核心，4.4 是主题中点名的配套取舍。

### 4.1 torch.compile 与动态形状

#### 4.1.1 概念说明

DeepSpec 训练的输入是变长序列 batch（右侧零填充，真实长度随 batch 变化），外加 DSpark 每次前向还要现场随机采样锚点（u4-l2），意味着进入模型的张量形状几乎每步都不同。如果用默认的静态编译，Dynamo 会为每个新形状重编译一次，编译开销很快吞掉 kernel 融合的收益，且撞上 `recompile_limit` 后静默回退 eager。

代码库给出的解法有两层：

1. **整体模型层**：`torch.compile(model, dynamic=True)`，把 batch 维之外的所有变化维度符号化，一份编译产物服务所有长度。
2. **局部函数层（Eagle3 路线）**：如果整体编译没开（Eagle3 的配置里 `torch_compile=False`），就单独对 `flex_attention` 和 `create_block_mask` 这两个热点函数各维护一个模块级编译单例，并把 `recompile_limit` 提到 64。

#### 4.1.2 核心流程

整体编译路径（DSpark + Qwen3 默认配置）：

```text
build_models() 产出裸 draft_model
    ↓ args.train.torch_compile == True ?
    ↓ 是 → torch.compile(model, dynamic=True)
    ↓ FSDP 包装（编译在里、FSDP 在外）
每个微批 forward：
    guard 评估（形状以符号量表达）→ 命中缓存 → Inductor kernel
    偶发 guard 失败（新形状特化）→ 重编译（计入 recompile_limit）
```

局部编译路径（Eagle3）：

```text
模型整体不编译
注意力层调用 compile_friendly_flex_attention：
    若正处于外层 Dynamo 追踪中（is_torchdynamo_compiling()）
        → 直接调用原生 flex_attention（避免嵌套编译造成 graph break）
    否则
        → 取模块级单例 _COMPILED_FLEX_ATTENTION（首次访问时创建）
```

#### 4.1.3 源码精读

**整体编译的唯一调用点**在 BaseTrainer 装配流程中，紧跟模型构建、先于 FSDP 包装：

[deepspec/trainer/base_trainer.py:185-188](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L185-L188)

```python
if self.args.train.torch_compile:
    print_on_local_main("Compiling training model with torch.compile...")
    self.model = torch.compile(self.model, dynamic=True)
self.model = self._wrap_with_fsdp(self.model)
```

这段代码做了两件事：配置键 `train.torch_compile` 控制是否编译（于是它可以被 `--opts train.torch_compile=False` 直接覆盖，这是后面消融实验的第一个开关）；`dynamic=True` 把变长维度符号化。注意顺序——先编译再包 FSDP，配合后面会看到的 `use_orig_params=True`，这是 PyTorch 官方推荐的兼容组合。

**recompile_limit 的提升**是 Eagle3 侧一个只有三行的函数：

[deepspec/modeling/eagle3/common.py:44-46](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L44-L46)

```python
def configure_eagle3_flex_compile():
    if dynamo.config.recompile_limit < 64:
        dynamo.config.recompile_limit = 64
```

`dynamo.config.recompile_limit` 是全局的「最多重编译几次然后回退 eager」上限（PyTorch 默认为 8）。Eagle3 的 TTT 训练里 `q_len`、`past_seen_tokens`、序列长度组合多样，即使有 dynamic 符号化，仍可能产生多于 8 种需要特化的形状；8 次用完后 flex_attention 会掉回未编译执行，性能断崖。提到 64 就是给形状特化留出余量——代价是最多 64 份编译产物占用的内存与编译时间。

**编译单例与防嵌套**是本模块最精巧的一段：

[deepspec/modeling/eagle3/common.py:49-73](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L49-L73)

```python
_COMPILED_FLEX_ATTENTION = None

@torch.compiler.disable(recursive=False)
def get_compiled_flex_attention():
    global _COMPILED_FLEX_ATTENTION
    if _COMPILED_FLEX_ATTENTION is None:
        configure_eagle3_flex_compile()
        _COMPILED_FLEX_ATTENTION = torch.compile(flex_attention)
    return _COMPILED_FLEX_ATTENTION

def compile_friendly_flex_attention(query, key, value, **kwargs):
    flex_attention_func = (
        flex_attention if is_torchdynamo_compiling() else get_compiled_flex_attention()
    )
    return flex_attention_func(query, key, value, **kwargs)
```

三个设计点：

1. **模块级单例**：`torch.compile(flex_attention)` 只创建一次，全模型所有层共享同一份编译缓存，避免每层各自编译。
2. **`@torch.compiler.disable(recursive=False)`**：getter 本身对 Dynamo 不透明——外层编译追踪时不会试图把「创建编译器对象」这种代码画进图里造成 graph break；`recursive=False` 表示只豁免这一层函数，它内部返回的编译产物照常工作。
3. **`is_torchdynamo_compiling()` 分支**：当外层已经在 Dynamo 编译中（`transformers.utils` 提供的探测函数），就退回原生 `flex_attention`。因为 `flex_attention` 本身是编译友好的 autograd Function，让它被外层图直接吸收，优于在编译图里嵌套调用另一个编译入口。

同样的三件套对 `create_block_mask` 又复制了一份（[deepspec/modeling/eagle3/common.py:76-100](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L76-L100)），此处不再展开。

**调用侧的长度阈值**——Eagle3 注意力层并非无条件走编译版：

[deepspec/modeling/eagle3/qwen3/modeling.py:107-124](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L107-L124)

```python
if self.config._attn_implementation == "flex_attention":
    assert attention_mask is not None, (
        "Eagle3 flex_attention expects a BlockMask attention_mask."
    )
    flex_attention_func = (
        flex_attention
        if int(q_len) <= 128
        else compile_friendly_flex_attention
    )
    attn_output = flex_attention_func(
        query=q, key=k.contiguous(), value=v.contiguous(),
        block_mask=attention_mask,
        enable_gqa=True,
    )
```

`q_len <= 128` 的短序列直接用原生 `flex_attention`，长序列才动用编译单例——小图上编译的固定开销摊不平。注意这里 `enable_gqa=True` 原生支持 GQA；而 4.2.3 会看到 DSpark 走 transformers 通用分发表时必须先把 K/V 手工展开。掩码构造侧同样有这个 128 阈值（[deepspec/modeling/eagle3/common.py:129-131](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L129-L131)）。

**配置层的真实取值**——性能开关不是纸面选项，各配置文件里的实际选择如下（grep 全部 12 份配置可得）：

- DSpark/DFlash + Qwen3：`torch_compile=True`，如 [config/dspark/dspark_qwen3_4b.py:43-44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L43-L44)（`sharding_strategy="no_shard"`、`torch_compile=True`）。
- Eagle3 全部配置与 DSpark/DFlash + Gemma4 配置：`torch_compile=False`（如 [config/eagle3/eagle3_qwen3_4b.py:29-30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L29-L30)）。Eagle3 不开整体编译却仍有性能手段，靠的正是上面那套局部编译单例——这就是两条路线的分工。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `dynamic=True` 与默认静态编译在「变长输入」下重编译次数的差别。

**操作步骤**（以下为示例代码，独立小脚本，不依赖 DeepSpec，只需安装 torch≥2.5）：

```python
# demo_recompile.py —— 示例代码
import torch, torch._dynamo as dynamo

model = torch.nn.Sequential(
    torch.nn.Linear(64, 128), torch.nn.GELU(), torch.nn.Linear(128, 64)
).cuda()

def run(compiled):
    dynamo.reset()
    for seq_len in [64, 100, 128, 200, 256, 300]:
        x = torch.randn(2, seq_len, 64, device="cuda")
        compiled(x).sum().backward()
    return dynamo.utils.counters["stats"]["unique_graphs"]

static = torch.compile(model)                # 静态：按具体形状特化
dynamic = torch.compile(model, dynamic=True) # 动态：维度符号化
print("static unique graphs :", run(static))
print("dynamic unique graphs:", run(dynamic))
```

**需要观察的现象**：静态版对 6 个不同长度会得到多个 unique graph（每个新长度一次特化）；动态版通常只有 1–2 个（首步可能先按静态特化、再泛化成符号图）。

**预期结果**：`static unique graphs` 明显大于 `dynamic unique graphs`。若想看每次重编译的原因，可用环境变量 `TORCH_LOGS=recompiles` 运行。具体数值随 torch 版本变化，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `configure_eagle3_flex_compile` 写成 `if dynamo.config.recompile_limit < 64` 而不是无条件赋值 64？

**答案**：这是一个「只升不降」的防御式写作：若外部环境（或其他代码）已经设置了更大的上限，不应把它改小；只有当前值低于 64 时才提升。

**练习 2**：DSpark 配置里 `torch_compile=True`，那么 DSpark 还需要 `compile_friendly_flex_attention` 这套单例吗？从代码事实出发回答。

**答案**：不需要走这套单例。DSpark 的注意力经 transformers 的 `ALL_ATTENTION_FUNCTIONS` 分发（见 4.2.3），`flex_attention` 会被外层整体编译直接追踪进图——这正是 `is_torchdynamo_compiling()` 分支存在的意义：整体编译生效时永远取「原生 flex_attention」这支。单例路线是给「整体编译关闭」的 Eagle3 准备的。

**练习 3**：`recompile_limit` 提到 64 有什么代价？

**答案**：最多允许 64 份形状特化的编译产物，编译时间与缓存内存都可能增大；极端情况下大量一次性形状会让训练前几十步都在编译。它是「避免回退 eager 的保险」，不是越大越好。

### 4.2 flex_attention 与 BlockMask：块稀疏注意力

#### 4.2.1 概念说明

u4-l1 讲过 DSpark 的注意力规则：每个草稿 query 只能看到「锚点左侧的上下文 ∪ 自己所在的噪声块」，块与块之间互相隔离。这是一种**非因果、非规则**的稀疏模式。承载它有三种候选：

| 承载方式 | 显存 | 计算 | 说明 |
| --- | --- | --- | --- |
| 稠密 4D 加性掩码（float 的 `[B,1,Q,KV]`） | 小（可广播） | 全量 QK^T，被掩位置也参与计算再被减成 −inf | 掩码形状还得随 batch 变化 |
| eager 显式注意力权重 | \(O(B \cdot H \cdot Q \cdot KV)\) | 全量 + 大量访存 | 最直白也最贵 |
| **BlockMask + flex_attention** | 块级索引，很小 | **整块跳过**被掩区域 | 掩码语义写成 `mask_mod` 闭包 |

`BlockMask` 不存每个 (q, kv) 位置的布尔值，而是把序列切成 128×128 的块，只记「每个 query 块对应哪些 kv 块可见」的索引。`flex_attention` 编译后的 Triton kernel 对不可见块**直接不发射计算**，收益来自两处：跳过的 FLOPs 与跳过的访存。这对 DSpark 尤其重要——草稿块内的 query 数只有 `block_size=7` 个，传统 attention kernel 面对大量「几乎全空」的行浪费严重。

粗略估算（示例推算，假设 `seq_len=4096`、`num_anchors×block_size=448`、锚点在序列内均匀分布）：每个 query 平均可见的 KV 长度约为 \( \bar{a} + 7 \)（\(\bar{a}\) 为平均锚点位置 ≈ 2048），而总 KV 长度为 4544，可见占比约 45%——即便不做任何掩码优化也有一半以上的注意力计算是纯浪费；块稀疏让这部分被整体跳过。

#### 4.2.2 核心流程

```text
mask_mod(b, h, q_idx, kv_idx) -> bool     # 用纯 tensor 运算描述可见性
        ↓ create_block_mask(mask_mod, B, H, Q_LEN, KV_LEN)
BlockMask（块级稀疏索引，形状随 Q_LEN/KV_LEN 划分成 128 块）
        ↓ flex_attention(q, k, v, block_mask=BlockMask)
编译后的 Triton kernel：逐 query 块只对可见 kv 块计算
```

DSpark 的调用链：`forward` 里现场采样锚点 → `create_dspark_attention_mask` 产出 BlockMask → 逐层传入注意力，注意力层按 `_attn_implementation` 分发到 flex_attention。

#### 4.2.3 源码精读

**DSpark 的 mask_mod 与 BlockMask 构造**：

[deepspec/modeling/dspark/common.py:78-106](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L78-L106)

```python
def create_dspark_attention_mask(*, anchor_positions, block_keep_mask,
                                 seq_len, block_size, device):
    def dspark_mask_mod(b, h, q_idx, kv_idx):
        del h
        q_block_id = q_idx // block_size
        anchor_pos = anchor_positions[b, q_block_id]
        is_context = kv_idx < seq_len
        mask_context = is_context & (kv_idx < anchor_pos)   # 上下文：锚点左侧
        is_draft = kv_idx >= seq_len
        kv_block_id = (kv_idx - seq_len) // block_size
        mask_draft = is_draft & (q_block_id == kv_block_id) # 草稿：仅本块
        is_valid_block = block_keep_mask[b, q_block_id]
        return (mask_context | mask_draft) & is_valid_block
    ...
    return create_block_mask(
        dspark_mask_mod, B=bsz, H=None,
        Q_LEN=num_blocks * block_size,
        KV_LEN=seq_len + num_blocks * block_size,
        device=device,
    )
```

闭包 `dspark_mask_mod` 就是 u4-l1 那条构图规则的直接翻译：`(上下文且在锚点左侧) 或 (草稿且同块)`，再与块有效性相与。`create_block_mask` 会对这个闭包做向量化评测并压缩成块级索引。注意 `H=None` 表示掩码对所有注意力头广播。该掩码在模型 `forward` 中每步重建（锚点每步随机采样），调用点在 [deepspec/modeling/dspark/qwen3/modeling.py:415-421](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L415-L421)——当整体 `torch.compile(dynamic=True)` 生效时，这次构造也被 Dynamo 追踪进编译图，由符号形状吸收每步的长度变化。

**DSpark 注意力层的分发与 GQA 展开**：

[deepspec/modeling/dspark/qwen3/modeling.py:120-136](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L120-L136)

```python
if (
    self.config._attn_implementation == "flex_attention"
    and self.num_key_value_groups > 1
):
    kv_seq_len = k.shape[-2]
    k = k.repeat_interleave(self.num_key_value_groups, dim=1)
    v = v.repeat_interleave(self.num_key_value_groups, dim=1)
    ...
attn_fn: Callable = eager_attention_forward
if self.config._attn_implementation != "eager":
    attn_fn = ALL_ATTENTION_FUNCTIONS[self.config._attn_implementation]
```

两个信息：DSpark 走 transformers 的通用注意力分发表，该路径下的 flex_attention 不支持 GQA 直通，所以先把 K/V 用 `repeat_interleave` 展开到与 Q 相同的头数（对比 4.1.3 中 Eagle3 直调时 `enable_gqa=True` 免展开）；`_attn_implementation` 来自各模型族 config 模块里的常量 `TRAIN_ATTN_IMPLEMENTATION = "flex_attention"`，经 [deepspec/modeling/dspark/qwen3/config.py:44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L44) 写入草稿 config。**要换成 eager/sdpa，改的就是这个模块常量**（消融实验的第二个开关，见第 5 节）。

**Eagle3 的 mask_mod 与稠密回退对照**。Eagle3 的可见性规则是「首块因果 ∪ 后续块同索引对角线」（TTT 链式对齐，u5-l1）：

[deepspec/modeling/eagle3/common.py:116-126](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L116-L126)

```python
def eagle3_mask_mod(b, h, q_idx, kv_idx):
    del h
    seq_len = seq_lengths[b]
    in_valid_query = q_idx < seq_len
    causal_mask = (q_idx >= kv_idx) & (kv_idx < seq_len)
    suffix_mask = (
        (kv_idx >= q_len)
        & ((kv_idx % q_len) < seq_len)
        & (((kv_idx - q_idx) % q_len) == 0)
    )
    return in_valid_query & (causal_mask | suffix_mask)
```

同一个文件里保留了稠密掩码的对照实现 `prepare_4d_causal_attention_mask`（[deepspec/modeling/eagle3/common.py:142-171](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L142-L171)）：它构造 `[1,1,Q,KV]` 的加性 float 掩码（0 或 dtype 最小值），只有因果 + padding 语义。模型侧的分支在 [deepspec/modeling/eagle3/qwen3/modeling.py:274-302](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L274-L302)：`_attn_implementation == "flex_attention"` 才走 `create_eagle3_attention_mask`（BlockMask），否则退回稠密 4D 掩码。**注意**：这条稠密回退只表达因果 + padding，不含 `suffix_mask` 的跨块对角线，多步 TTT 下语义与 flex 路径不等价——它更像兼容通道而非等价实现，做「换 eager 测性能」的消融时必须意识到这一点（DSpark 的掩码规则简单，等价替换更容易，见第 5 节）。

**一个值得注意的族间差异**：DSpark 的 Gemma4 实现直接调用原生 `flex_attention`，没有经过编译单例包装（[deepspec/modeling/dspark/gemma4/modeling.py:145-147](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L145-L147)），且 Gemma4 配置 `torch_compile=False`。也就是说不同「算法 × 模型族」组合实际激活的性能路径并不相同，读配置时要把 `torch_compile`、`TRAIN_ATTN_IMPLEMENTATION` 和代码里是否走编译单例三件事合起来看。

#### 4.2.4 代码实践

**实践目标**：在脱离 DeepSpec 的最小环境里量化 BlockMask 相对稠密掩码的收益。

**操作步骤**（示例代码）：

```python
# demo_blockmask.py —— 示例代码
import torch, time
from torch.nn.attention.flex_attention import flex_attention, create_block_mask, create_mask

torch.manual_seed(0)
B, H, Q, KV, D = 2, 32, 448, 4544, 128
q = torch.randn(B, H, Q, D, device="cuda")
k = torch.randn(B, H, KV, D, device="cuda")
v = torch.randn(B, H, KV, D, device="cuda")
anchor = 2048

def mod(b, h, q_idx, kv_idx):
    return (kv_idx < anchor) | ((kv_idx >= anchor) & (q_idx < 7) & (kv_idx < anchor + 7))

block_mask = create_block_mask(mod, B=B, H=None, Q_LEN=Q, KV_LEN=KV, device="cuda")
dense_bool = create_mask(mod, B=B, H=None, Q_LEN=Q, KV_LEN=KV, device="cuda")
dense_add = torch.where(dense_bool, 0.0, float("-inf")).to(q.dtype)  # [B,1,Q,KV]

flex_c = torch.compile(flex_attention)
sdpa = torch.nn.functional.scaled_dot_product_attention

def bench(fn, n=20):
    for _ in range(5): fn()
    torch.cuda.synchronize(); t = time.perf_counter()
    for _ in range(n): fn()
    torch.cuda.synchronize(); return (time.perf_counter() - t) / n * 1e3

print(f"flex+BlockMask : {bench(lambda: flex_c(q, k, v, block_mask=block_mask)):.2f} ms")
print(f"sdpa+dense     : {bench(lambda: sdpa(q, k, v, attn_mask=dense_add)):.2f} ms")
```

**需要观察的现象**：两条路径的毫秒数差异；另外打印 `dense_add` 的元素数（`B*Q*KV`）与 BlockMask 的块索引规模作对比。

**预期结果**：在掩码稀疏度较高（本例可见率约 45%）时编译版 flex 通常快于稠密 SDPA；稀疏度越高差距越大，序列越长块稀疏的跳过优势越明显。具体倍率依赖 GPU 型号与 torch 版本，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`create_block_mask` 为什么要求 `mask_mod` 只用 tensor 运算书写、且捕获的变量（如 `anchor_positions`）是 tensor？

**答案**：`create_block_mask` 不会逐位置调用 Python 闭包，而是把 `q_idx/kv_idx` 构造成广播的索引张量后**一次性向量化求值**整个 `[B,H,Q,KV]` 布尔矩阵，再压缩成块索引。闭包里若有 Python 分支或标量循环无法这样向量化；捕获 tensor 则让同一份 mask_mod 可随 batch 数据变化。

**练习 2**：DSpark 每个训练步都重新调用 `create_block_mask`（锚点每步重采样），这不怕慢吗？

**答案**：两层保护。其一，`create_block_mask` 本身是向量化评测 + 块压缩，远快于逐元素构造稠密掩码；其二，DSpark 默认 `torch_compile=True`，掩码构造发生在被编译的 `forward` 内部，形状由 dynamic 符号化吸收，只有内容变化而无需重编译。Eagle3 则对 `create_block_mask` 另行维护了编译单例并设了 `q_len<=128` 的免编译阈值。

**练习 3**：如果把 `_attn_implementation` 改成 `"sdpa"` 但仍传入 BlockMask，会发生什么？

**答案**：会出错。SDPA/eager 期望加性 float 掩码，`BlockMask` 是专门给 flex_attention 的结构化对象，无法参与 SDPA 的掩码加法。DSpark 的 forward 无条件构造 BlockMask（qwen3/modeling.py:415-421），因此「换 eager/sdpa」的消融必须连同掩码构造一起换成稠密版本，只拨一个开关是不够的——这正是第 5 节综合实践里第二个变体的操作要点。

### 4.3 CUDA stream 预取与 no_sync

#### 4.3.1 概念说明

训练一个微批的串行时间线是三段之和：

\[ T_{\text{串行}} = t_{\text{取数}}(\text{DataLoader 读缓存+collate}) + t_{\text{H2D}}(\text{CPU}\to\text{GPU 拷贝}) + t_{\text{计算}}(\text{forward+backward}) \]

三段用的是三种不同资源（CPU worker、PCIe/NVLink、GPU SM），串行执行是纯浪费。`CUDAPrefetcher` 构造**双缓冲**：当 GPU 计算第 i 个 batch 时，后台线程已经在 CPU 上取第 i+1 个 batch、并在独立 CUDA stream 上发起它的 H2D 拷贝。管线充满后每步时间逼近：

\[ T_{\text{重叠}} \approx \max(t_{\text{取数}},\; t_{\text{H2D}},\; t_{\text{计算}}) \]

`no_sync` 解决的是另一段浪费：FSDP 默认（含 `NO_SHARD` 的 DDP 式行为）在**每个** backward 后做一次全 rank 梯度规约；梯度累积的 G−1 个微批其实只需要本地累加。包进 `no_sync()` 后，G 个微批只在最后一个（同步微批）backward 时做一次规约，通信次数从 G 次降为 1 次，且规约的等待不再打断 G−1 个微批的计算流。

#### 4.3.2 核心流程

CUDAPrefetcher 的时序（下标 i 为 batch 序号）：

```text
__iter__：同步取 batch_0 并在 side stream 发起 H2D（保证首个 __next__ 有货）
循环体：
  __next__ 第 i 次：
    1. join 上一轮启动的后台线程（此时 batch_{i+1} 的取数+H2D 已完成）
    2. current.wait_stream(side)：计算流等待 side stream 的拷贝完成
    3. 对 batch_i 的每个 tensor record_stream(current)：锁住显存不被提前复用
    4. 启动新后台线程：取 batch_{i+2} 并在 side stream 发起 H2D
    5. 返回 batch_i → 主线程计算，与第 4 步完全并行
```

no_sync 在主循环中的时序（G=4 为例）：

```text
micro 0,1,2：with model.no_sync(): forward + backward（梯度本卡累加，零通信）
micro 3（should_sync=True）：裸上下文 forward + backward
          → backward 末尾一次性规约整份累计梯度
          → FSDP.clip_grad_norm_ → optimizer.step()
```

#### 4.3.3 源码精读

**搬运函数：异步拷贝与 GPU 侧类型转换**：

[deepspec/data/cuda_prefetcher.py:6-11](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L6-L11)

```python
def move_batch_to_device(batch, device):
    moved = {key: value.to(device, non_blocking=True) for key, value in batch.items()}
    # Embedding lookup requires int64; cast on GPU to avoid bloating CPU-to-GPU transfer.
    if moved["input_ids"].dtype != torch.long:
        moved["input_ids"] = moved["input_ids"].to(torch.long)
    return moved
```

`non_blocking=True` 是异步 H2D 的前提（配合 pin_memory，见下）；`input_ids` 先按协议里的 int32 传输、到 GPU 再转 int64——把类型转换放到带宽便宜的设备侧，注释里写明了动机（u2-l4/u2-l6 讲过缓存里 token 是 int32）。

**预取器骨架与首个 batch**：

[deepspec/data/cuda_prefetcher.py:22-34](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L22-L34)

```python
def __init__(self, dataloader, device):
    self.dataloader = dataloader
    self.device = device
    self.stream = torch.cuda.Stream(device=device)

def __iter__(self):
    self._iter = iter(self.dataloader)
    self._done = False
    self._gpu_batch = None
    self._thread = None
    # First batch: fetch synchronously so __next__ has something to return.
    self._fetch_and_transfer()
    return self
```

构造时创建**专属 side stream**；迭代开始时同步取首个 batch，保证第一次 `__next__` 立即有数据——管线是从第二步开始才完全重叠的。

**后台取数与 H2D**：

[deepspec/data/cuda_prefetcher.py:36-44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L36-L44)

```python
def _fetch_and_transfer(self):
    try:
        cpu_batch = next(self._iter)
    except StopIteration:
        self._done = True
        return
    with torch.cuda.stream(self.stream):
        self._gpu_batch = move_batch_to_device(cpu_batch, self.device)
```

`next(self._iter)` 在 DataLoader 的 worker 进程侧完成读缓存与 collate；`with torch.cuda.stream(self.stream)` 让其后的 `.to(device, non_blocking=True)` 排到 side stream 而非默认计算流——这是「拷贝与计算并行」的直接实现。

**同步三连：join → wait_stream → record_stream，再踢出下一轮**：

[deepspec/data/cuda_prefetcher.py:46-70](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L46-L70)

```python
def __next__(self):
    if self._thread is not None:
        self._thread.join()
        self._thread = None
    if self._done:
        raise StopIteration
    current = torch.cuda.current_stream(self.device)
    current.wait_stream(self.stream)
    batch = self._gpu_batch
    for value in batch.values():
        value.record_stream(current)
    self._thread = Thread(target=self._fetch_and_transfer, daemon=True)
    self._thread.start()
    return batch
```

四个动作各有含义：`join` 等 CPU 侧取数结束（拿到 `self._gpu_batch` 的最终指向）；`wait_stream` 建立计算流对 side stream 的依赖（H2D 完成才能算）；`record_stream` 告诉缓存分配器这些目标显存还被计算流引用——没有它，`_fetch_and_transfer` 下一轮分配新 tensor 时旧显存可能被复用覆盖（异步拷贝下经典 use-after-free 陷阱，u2-l6 已从协议角度提过）；最后 `daemon` 线程启动下一轮取数+拷贝，与返回 batch 的计算并行。

**消费侧：DataLoader 的三层供给参数**：

[deepspec/trainer/base_trainer.py:304-314](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L304-L314)

```python
return DataLoader(
    self.train_dataset,
    batch_size=int(self.args.train.local_batch_size),
    sampler=sampler,
    collate_fn=self.data_collator_cls(),
    num_workers=int(self.args.data.num_workers),
    pin_memory=True,
    drop_last=True,
    persistent_workers=True,
    prefetch_factor=4,
)
```

注意性能供给其实是**两层**：DataLoader 层（`num_workers` 个 CPU 进程 + `prefetch_factor=4` 的进程内队列 + `persistent_workers` 免去每 epoch 重启进程）负责把 CPU 侧 batch 提前备好；`pin_memory=True` 分配页锁定内存，是 `non_blocking=True` 异步 H2D 的物理前提。CUDAPrefetcher 在其上再补第三层：把「取出 CPU batch + 发起 H2D」也藏进计算的背后。

**主循环：prefetcher 与 no_sync 的合流点**：

[deepspec/trainer/base_trainer.py:373-390](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L373-L390)

```python
for batch in prefetcher:
    should_sync = (
        (self.next_micro_step + 1) % self.gradient_accumulation_steps == 0
    )
    sync_context = nullcontext() if should_sync else self.model.no_sync()
    with sync_context:
        loss = self.run_batch(batch) / self.gradient_accumulation_steps
        loss.backward()
    self.next_micro_step += 1
    if not should_sync:
        continue
    grad_norm = FSDP.clip_grad_norm_(...)
    self.optimizer.step()
```

`for batch in prefetcher`（实例化在 [deepspec/trainer/base_trainer.py:369](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L369)）让每个微批的数据在计算期间预取；`should_sync` 分流决定本微批 backward 是否触发通信。裁剪与 `optimizer.step()` 只发生在同步微批（[deepspec/trainer/base_trainer.py:386-390](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L386-L390)），与 u3-l2 讲过的「next_micro_step 是唯一真相源」一致。

#### 4.3.4 代码实践

**实践目标**：用最小例子直观感受 stream 重叠，学会用 `torch.cuda.Event` 计时。

**操作步骤**（示例代码）：

```python
# demo_stream.py —— 示例代码
import torch, time

dev = "cuda"
side = torch.cuda.Stream(device=dev)
big = [torch.randn(1024, 1024, 4096, pin_memory=True) for _ in range(4)]  # ~64MB/个

def gpu_heavy():
    a = torch.randn(8192, 8192, device=dev)
    for _ in range(30):
        a = a @ a
    return a

# 串行：拷贝完再算
torch.cuda.synchronize(); t0 = time.perf_counter()
for x in big:
    y = x.to(dev, non_blocking=True)
    torch.cuda.synchronize()          # 等拷贝
    gpu_heavy()
torch.cuda.synchronize()
t_serial = time.perf_counter() - t0

# 重叠：side stream 拷贝，主流计算
torch.cuda.synchronize(); t0 = time.perf_counter()
cur = torch.cuda.current_stream(dev)
for x in big:
    with torch.cuda.stream(side):
        y = x.to(dev, non_blocking=True)
    cur.wait_stream(side)
    y.record_stream(cur)
    gpu_heavy()
torch.cuda.synchronize()
t_overlap = time.perf_counter() - t0

print(f"serial : {t_serial*1e3:.1f} ms")
print(f"overlap: {t_overlap*1e3:.1f} ms")
```

**需要观察的现象**：两段总耗时差异；可用 `nvidia-smi -u ms` 或 Ns Systems 观察拷贝与计算是否在时间轴上交叠。

**预期结果**：overlap 版明显快于 serial 版（拷贝被计算掩盖）。若差距小，说明 `gpu_heavy` 太轻或拷贝太大，可调整两者比例再观察。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：去掉 `record_stream` 会发生什么？为什么平时不易暴露？

**答案**：side stream 写入的目标显存在主线程下一次分配时可能被缓存分配器复用，异步 H2D 尚未完成或计算尚未读取时数据被覆盖，出现偶发错数。不易暴露是因为复用恰好命中的概率低、且错误表现为精度下降而非崩溃——正是最难排查的一类 bug，所以源码注释专门强调。

**练习 2**：`pin_memory=True` 与 `non_blocking=True` 是什么关系？

**答案**：异步 H2D（`non_blocking=True`）需要源内存是页锁定（pinned）的，否则 CUDA 驱动只能走同步的暂存路径。DataLoader 的 `pin_memory=True` 保证交给 `move_batch_to_device` 的 CPU tensor 已在锁页内存中，二者缺一不可。

**练习 3**：为什么 `no_sync` 能省时间？省的是带宽还是次数？

**答案**：主要省次数与同步等待。梯度总量不变（最终仍要规约一份完整累计梯度），但通信发起从每优化器步 G 次降到 1 次；更重要的是 G−1 个微批的 backward 不再被规约的同步点打断，计算流密度更高。当 G 较大（DeepSpec 默认配置下 G = global_batch/(world_size×local_batch)，可达数十）时收益显著。

### 4.4 FSDP sharding_strategy 的选择

#### 4.4.1 概念说明

FSDP 的 `sharding_strategy` 决定「参数、梯度、优化器状态」在卡间如何切分（以下为 PyTorch FSDP 通识语义）：

| 策略 | 参数 | 梯度 | 每卡显存（可训练部分） | 通信 |
| --- | --- | --- | --- | --- |
| `full_shard` | 分片，用时 all-gather | 分片 reduce-scatter | \(M/N\) | all-gather + reduce-scatter |
| `shard_grad_op` | 完整复制 | 分片 | \(P + M_g/N\) | reduce-scatter |
| `no_shard`（DDP 式） | 完整复制 | 完整复制 | \(M\) | 梯度 all-reduce |
| `hybrid_shard` | 节点内分片、节点间复制 | 同左 | \(M/\text{节点内卡数}\) | 节点内 collectives |

其中 \(M\) 为单卡完整副本的显存开销。DeepSpec **全部 12 份配置都用 `no_shard`**。原因要用 u3-l1 的结论才能看懂：草稿模型的 `embed_tokens` 与 `lm_head` 直接冻结复用目标模型权重，**可训练参数只有几层草稿主干**（DSpark 5 层、Eagle3 1 层），\(M\) 本来就小；而分片路线要为每层 forward 付出 all-gather 延迟、为保存 checkpoint 增加聚合复杂度。对小模型，切分省下的显存买不回通信与延迟——`no_shard` 让 FSDP 退化为一个统一的分布式外壳（梯度 all-reduce + 统一的 clip_grad_norm_ 接口），这正是 u3-l1 说的「只作外壳」。

#### 4.4.2 核心流程

```text
config: train.sharding_strategy = "no_shard"
    ↓ _wrap_with_fsdp(model)
    ↓ _build_fsdp_kwargs 查 _SHARDING_STRATEGIES 映射表
    ↓ 通用参数：use_orig_params=True + MixedPrecision(bf16)
    ↓ 若是 hybrid 策略：额外构造 device_mesh（节点内 shard、节点间 replicate）
    ↓ FSDP(model, **kwargs)
```

#### 4.4.3 源码精读

**策略名到枚举的映射表**：

[deepspec/trainer/base_trainer.py:40-52](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L40-L52)

```python
_SHARDING_STRATEGIES = {
    "full_shard": ShardingStrategy.FULL_SHARD,
    "shard_grad_op": ShardingStrategy.SHARD_GRAD_OP,
    "no_shard": ShardingStrategy.NO_SHARD,
    "hybrid_shard": ShardingStrategy.HYBRID_SHARD,
    "hybrid_shard_zero2": ShardingStrategy._HYBRID_SHARD_ZERO2,
    ...
}
_HYBRID_STRATEGIES = (ShardingStrategy.HYBRID_SHARD, ShardingStrategy._HYBRID_SHARD_ZERO2)
```

字符串键意味着策略可以直接用 `--opts train.sharding_strategy=full_shard` 切换，不需要改代码。

**FSDP 关键字构造**：

[deepspec/trainer/base_trainer.py:55-74](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L55-L74)

```python
def _build_fsdp_kwargs(*, sharding_strategy_name, precision_dtype, world_size):
    sharding_strategy = _SHARDING_STRATEGIES[sharding_strategy_name]
    fsdp_kwargs = dict(
        use_orig_params=True,
        mixed_precision=MixedPrecision(
            param_dtype=precision_dtype,
            buffer_dtype=precision_dtype,
        ),
        sharding_strategy=sharding_strategy,
    )
    if sharding_strategy in _HYBRID_STRATEGIES:
        devices_per_node = torch.cuda.device_count()
        fsdp_kwargs["device_mesh"] = init_device_mesh(
            "cuda",
            (world_size // devices_per_node, devices_per_node),
            mesh_dim_names=("replicate", "shard"),
        )
    return fsdp_kwargs
```

三个要点：`use_orig_params=True` 让 FSDP 包装后原 `Parameter` 对象仍然可见——这是 BaseTrainer 把 `BF16Optimizer` 直接建在**未包装**的 `draft_model` 上仍能正确更新权重的原因（u3-l1），也与 torch.compile 兼容；`MixedPrecision(param_dtype=bf16)` 表示前向/反向用 bf16，而 fp32 主权重由 `BF16Optimizer` 自管（u3-l4），分工明确；hybrid 策略需要显式构造二维 device_mesh（节点间 replicate 维 + 节点内 shard 维），`no_shard` 则什么都不加。包装本身只有一行（[deepspec/trainer/base_trainer.py:287-293](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L287-L293)）。配置默认值见 4.1.3 已引用的 [config/dspark/dspark_qwen3_4b.py:43-44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L43-L44)。

#### 4.4.4 代码实践

**实践目标**：建立「策略 → 每卡显存」的量级直觉。

**操作步骤**（示例代码，只算账不跑模型）。设可训练参数量为 \(P\)（bf16 权重 + bf16 梯度 + fp32 主权重 + 两个 fp32 Adam 矩）：

\[ M_{\text{no\_shard}} = P \times (2 + 2 + 4 + 4 + 4) = 16P \ \text{字节/卡}, \qquad M_{\text{full\_shard}} \approx \frac{12P}{N} + 2P \]

```python
# demo_shard_mem.py —— 示例代码
def per_gpu_bytes(P_params, world_size, strategy):
    weights_bf16 = 2 * P_params
    grads_bf16   = 2 * P_params
    master_fp32  = 4 * P_params
    adam_moments = 8 * P_params          # fp32 一阶 + 二阶
    if strategy == "no_shard":
        return weights_bf16 + grads_bf16 + master_fp32 + adam_moments
    if strategy == "full_shard":         # 权重与梯度计算时短暂聚合，稳态分片
        return (weights_bf16 + grads_bf16 + master_fp32 + adam_moments) / world_size

for P in [50e6, 500e6, 5e9]:             # 5千万 / 5亿 / 50亿可训练参数
    for ws in [8, 64]:
        print(f"P={P:.0e} ws={ws:2d} no_shard={per_gpu_bytes(P, ws, 'no_shard')/2**30:6.2f} GiB"
              f"  full_shard={per_gpu_bytes(P, ws, 'full_shard')/2**30:6.2f} GiB")
```

**需要观察的现象**：找出「full_shard 开始显著省显存」的 \(P\) 阈值。

**预期结果**：\(P\) 在千万~亿级（DeepSpec 草稿主干的量级）时两种策略每卡差异只有几百 MiB 以内，省显存不足以抵消 all-gather 开销；\(P\) 达到数十亿且卡数多时差距才拉开。这就解释了默认 `no_shard` 的选择。

#### 4.4.5 小练习与答案

**练习 1**：既然 `no_shard`，那 FSDP 在这里还提供什么价值？换成裸 DDP 行不行？

**答案**：它仍提供：统一的梯度规约、`FSDP.clip_grad_norm_` 全局范数接口、`no_sync` 语义、与 `use_orig_params` 配合的权重导出路径。理论上 DDP 也能覆盖大部分，但 FSDP 保留了随时切到 `full_shard` 的能力（一行 `--opts`），为将来训练更大的草稿主干留了余地。

**练习 2**：`MixedPrecision(param_dtype=bf16)` 与 `BF16Optimizer` 的 fp32 主权重是否重复？

**答案**：不重复，分工不同。FSDP 的 MixedPrecision 控制的是**模块前向/反向时参数与 buffer 以什么 dtype 参与计算**（bf16 算得快）；`BF16Optimizer` 管的是**参数更新的数值精度**（fp32 主权重 + Adam 矩，避免微小更新被 bf16 舍入吞掉，u3-l4）。

**练习 3**：为什么 hybrid 策略需要 `init_device_mesh` 而 `no_shard` 不需要？

**答案**：hybrid 的「节点内分片、节点间复制」必须知道哪些 rank 同属一节点，这由二维 mesh（replicate × shard）显式表达；`no_shard` 对所有 rank 一视同仁地复制并 all-reduce，无需任何分组信息。

## 5. 综合实践：三个性能开关的消融实验

这是本讲的毕业任务：把 4.1–4.3 的三个开关各自关掉一次，量化每个的贡献。**所有对 DeepSpec 源码的临时修改都只在你自己的 checkout 里做，测完恢复**（本讲义不改动仓库源码）。

### 5.1 实验设计

固定一个小规模环境（无 GPU 条件下可缩小到单卡 + 极小缓存）：

```bash
python train.py config/dspark/dspark_qwen3_4b.py --gpus 0 \
  --opts data.target_cache_path=<你的小缓存路径> \
         train.max_train_steps=20 \
         train.global_batch_size=<world_size×local_batch×1，令 G=1 或较小> \
         train.checkpointing_steps=1000 \
         logging.logging_steps=1
```

四个变体：

| 变体 | 改动方式 | 改动位置 |
| --- | --- | --- |
| A 基线 | 默认配置 | 无 |
| B 关 torch.compile | `--opts train.torch_compile=False` | 纯配置覆盖，无需改码 |
| C flex → 稠密掩码 + eager | ① `TRAIN_ATTN_IMPLEMENTATION = "eager"`；② 把 `create_dspark_attention_mask` 里的 `create_block_mask(...)` 换成 `create_mask(...)` 并用 `torch.where(mask_bool, 0.0, -inf)` 转成加性 float 掩码 | 自己 checkout 里的 `deepspec/modeling/dspark/qwen3/config.py:6` 与 `deepspec/modeling/dspark/common.py:99-106`（`create_mask` 与 `create_block_mask` 同样由 `torch.nn.attention.flex_attention` 导出，签名一致） |
| D 去 CUDAPrefetcher | 把 `train()` 里 `for batch in prefetcher` 改为 `for batch in dataloader:`，循环体内先 `batch = move_batch_to_device(batch, self.device)` | 自己 checkout 里的 `deepspec/trainer/base_trainer.py:369-373` |

测量代码（示例代码，加在 `run_batch` 前后或包一层计时装饰）：

```python
import time, torch
t0 = time.perf_counter()
loss = self.run_batch(batch) / self.gradient_accumulation_steps
loss.backward()
torch.cuda.synchronize()
step_ms = (time.perf_counter() - t0) * 1e3
peak_gib = torch.cuda.max_memory_allocated() / 2**30
```

记录表（示例模板）：

| 变体 | 预热步数（编译发生在前几步） | 稳态每微批耗时 ms | 显存峰值 GiB | 备注 |
| --- | --- | --- | --- | --- |
| A | | | | |
| B | | | | 与 A 的差 = torch.compile 贡献 |
| C | | | | 与 A 的差 = BlockMask+flex 贡献（掩码构造成本也计入） |
| D | | | | 与 A 的差 = 预取贡献（小模型/小 batch 下可能接近 0） |

### 5.2 操作要点与陷阱

1. **预热**：变体 A/C 的前几步包含编译开销，计时应从第 5 步之后开始取均值；比较「首步耗时」本身也是一个有意义的观测（编译税有多大）。
2. **公平性**：四个变体用同一份缓存、同一 `seed` 相关配置、同一张卡；每档至少跑 2–3 次取中位数。
3. **变体 C 的语义**：稠密掩码版本在掩码语义上与 BlockMask 等价（同一个 `dspark_mask_mod` 求值），但 eager 路径下 `attention_mask` 是加性 float 张量，务必确认形状能广播到 `[B, H, Q, KV]`；另外 GQA 不再需要 4.2.3 那段 `repeat_interleave`（eager/sdpa 内部处理），差异本身也是观测点。
4. **显存归因**：`torch.cuda.reset_peak_memory_stats()` 要在每个变体开始前调用；C 变体注意稠密掩码本身 `[B,1,Q,KV]` 的 bf16 体积（示例推算：Q=448、KV≈4544、B=8 时约 320 MiB，随 batch 线性增长）。
5. **预期结论方向**（**待本地验证**）：torch.compile 在稳态吞吐上贡献通常最大；flex→eager 的退化随序列变长与稀疏度升高而放大；CUDAPrefetcher 的贡献取决于 batch 体积与模型规模的比值——模型小、数据大时才明显。若某个开关在你的规模下贡献≈0，这本身就是有价值的结论，写进分析。

## 6. 本讲小结

- **torch.compile 两层用法**：DSpark+Qwen3 走整体 `torch.compile(model, dynamic=True)`，把变长维度符号化、配合 FSDP `use_orig_params=True`；Eagle3 与 Gemma4 整体不编译，改用模块级编译单例包装 `flex_attention`/`create_block_mask`，用 `@torch.compiler.disable(recursive=False)` 防嵌套追踪、`is_torchdynamo_compiling()` 分支防 graph break，并把 `recompile_limit` 提到 64 防止静默回退 eager。
- **BlockMask 是语义与性能的双重选择**：DSpark 的非因果构图（上下文 ∪ 本块、块间隔离）天然稀疏，`create_block_mask` 把 `mask_mod` 闭包压缩成块级索引，编译版 `flex_attention` 整块跳过不可见区域；换 eager/sdpa 必须连掩码一起换稠密版本，且 Eagle3 的稠密回退不含 TTT 对角线语义。
- **CUDAPrefetcher = 后台线程 + 独立 stream 的双缓冲**：`wait_stream` 建依赖、`record_stream` 防 use-after-free、首 batch 同步取；它叠在 DataLoader 的 `num_workers/prefetch_factor/pin_memory` 之上，把每步时间从三段之和压到三段之最大。
- **no_sync 合并通信**：G 个微批只在同步微批做一次梯度规约，通信次数从 G 降到 1，反向计算流不再被同步点打断。
- **FSDP 默认 no_shard 是模型规模决定的**：可训练参数只有几层草稿主干（embed/lm_head 冻结复用），切分省的显存买不回 all-gather 延迟；一行 `--opts` 即可切换策略，hybrid 路线还备好了 device_mesh。
- **性能开关要合起来读**：`config` 里的 `torch_compile`/`sharding_strategy`、模型族常量 `TRAIN_ATTN_IMPLEMENTATION`、代码里是否走编译单例，三者共同决定实际执行的内核路径。

## 7. 下一步学习建议

- 下一讲 **u7-l3 毕业实战**：把本讲的 `--opts` 缩参技巧、性能观测与断点续训（u3-l5）全部串起来，跑一次端到端小规模训练 + 评测。
- 想继续深挖本讲的某个点，建议按此顺序读源码：先通读 [deepspec/data/cuda_prefetcher.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py)（仅 74 行，配合 PyTorch 官方文档《CUDA Semantics》的 stream 章节）；再对照 SpecForge 的 `flex_attention.py`（Eagle3 源码注释标明改编自那里）理解编译单例的演化；最后在 PyTorch 文档里读 `flex_attention` 教程的「BlockMask 与性能」一节。
- 若你要给仓库提性能相关 PR，消融实验表（第 5 节）就是现成的 benchmark 方法论：单变量、预热后计时、多次取中位、同时报告吞吐与显存峰值。
