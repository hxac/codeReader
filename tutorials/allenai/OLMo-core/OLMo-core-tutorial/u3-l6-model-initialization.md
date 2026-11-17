# 模型初始化：权重从哪里来

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `TransformerConfig` 中 `init_method` / `init_seed` / `init_std` / `embedding_init_std` 四个字段分别控制什么，以及 `model.init_weights()` 的完整调度流程。
- 对比 `InitMethod` 六种初始化方案（`normal`、`llama`、`llama_depth`、`normalized`、`fan_in`、`nanochat`）各自的标准差公式与适用场景。
- 写出本轮新增的 `InitMethod.nanochat` 的谱条件公式 \(\mathrm{std} = \frac{1}{\sqrt{\mathrm{fan\_in}}}\cdot\min\left(1,\sqrt{\frac{\mathrm{fan\_out}}{\mathrm{fan\_in}}}\right)\)，并说明它覆盖了哪些组件（注意力、KDA、GatedDeltaNet、稠密/MoE 前馈）。
- 用 `init_seed` 与 `seed_all()` 解释并验证初始化的全局可复现性，理解流水线并行下的 seed 偏移。
- 知道如何为新参数类型自定义初始化方案。

## 2. 前置知识

- **为什么要关心初始化**：训练开始时权重若太大，前向几层后激活就会爆炸、loss 变成 NaN；若太小，梯度消失、模型学不动。初始化的目标是让激活和梯度的规模在深度、宽度变化时保持稳定。OLMo-core 把这套「标准差怎么定」的决策抽象成了 `InitMethod` 枚举。
- **截断正态分布（truncated normal）**：把正态分布中超出 \([-3\sigma, +3\sigma]\) 的样本截掉，避免个别特别大的权重。本库绝大多数初始化用的是它（`nn.init.trunc_normal_`）。
- **fan_in 与 fan_out**：一个形状为 `(out_features, in_features)` 的 `nn.Linear` 权重，`fan_in = in_features`（每个输出分量由多少个输入相加而来），`fan_out = out_features`（该矩阵有多少个输出）。经典结论：要让输出方差稳定，标准差应随 \(\sqrt{\mathrm{fan\_in}}\) 变化。
- **`torch.Generator`**：PyTorch 的「私有随机数发生器」。用它采样不会消耗全局随机状态，因此初始化可以被一个独立的 `init_seed` 精确控制，与数据加载、dropout 用的全局随机流互不干扰。
- **DTensor**：PyTorch 的分布式张量抽象，同一个逻辑张量在不同 rank 上只持有分片。初始化分布式（如张量并行）权重时，必须保证各 rank 拿到的分片能拼成与单机初始化一致的全量张量——这是 `_apply_init` 存在的原因。
- **前置讲义**：本讲依赖 u3-l2（`TransformerConfig` 的字段与工厂方法）。你不需要读过大模型训练代码，但应知道 `config.build()` 会返回一个真实的 `Transformer` 模块。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/olmo_core/nn/transformer/init.py` | 本讲主角：`InitMethod` 枚举与所有初始化函数（`init_linear`、`nanochat_std`、`init_linear_nanochat` 等） |
| `src/olmo_core/nn/transformer/config.py` | `TransformerConfig` 的四个初始化相关字段，`build()` 如何把它们传给模型 |
| `src/olmo_core/nn/transformer/model.py` | `Transformer.init_weights()`：初始化的总调度入口 |
| `src/olmo_core/nn/attention/__init__.py` | `Attention` / `FusedAttentionV2` 的 `init_weights()`，各初始化方法在注意力上的落地 |
| `src/olmo_core/nn/attention/recurrent.py` | `GatedDeltaNet`（线性注意力序列混合器）的初始化 |
| `src/olmo_core/nn/attention/kda.py` | `KimiDeltaAttention` 的初始化 |
| `src/olmo_core/utils.py` | `seed_all()`：全局 RNG 播种 |
| `src/olmo_core/train/train_module/transformer/common.py` | 训练模块在真实训练流程中调用 `init_weights` 的位置 |
| `src/test/nn/transformer/init_test.py` | 官方测试：fan_in / nanochat 的标准差断言、确定性断言，是本讲实践的重要依据 |

## 4. 核心概念与源码讲解

### 4.1 init 与参数初始化总览：从 Config 到 `init_weights()`

#### 4.1.1 概念说明

「模型初始化」回答的问题是：**`build()` 出来的那些权重张量里最初填的是什么数？**

在 OLMo-core 中，这件事分两层：

1. **配置层**：`TransformerConfig` 上的四个字段描述「用什么方法、什么种子、什么基准标准差」。
2. **执行层**：`Transformer.init_weights()` 按固定顺序遍历模型，把配置分发给 embedding、每个 block 的注意力/前馈/MoE、以及 LM head。各组件（如 `Attention`）收到 `InitMethod` 后自己决定每个投影矩阵的具体标准差。

一个容易踩的坑：**`config.build()` 本身不会填充权重**。模型的 `__init__` 只是把 `init_method`、`init_seed` 等存为属性（见 [model.py:189-196](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L189-L196)），真正的数值要等你显式调用 `model.init_weights(...)`。在训练流程中，这一步由训练模块完成（见 4.1.2）；在单元测试和本讲实践中，你需要自己调用它。

#### 4.1.2 核心流程

`init_weights()` 的执行顺序：

```text
model.init_weights(device, world_mesh=None, model_part_idx=0)
  ├─ 1. to_empty(device)               # 在目标设备上重新分配（未初始化的）参数存储
  ├─ 2. 逐模块调用 reset_parameters()   # 无参模块（LayerNorm、RoPE 等）用 PyTorch 默认初始化
  ├─ 3. 计算 seed：
  │      seed = init_seed
  │      若启用流水线并行: seed += pp_local_rank + model_part_idx × pp_size
  ├─ 4. generator = torch.Generator(device).manual_seed(seed)   # 本模型专属随机流
  ├─ 5. 初始化 embeddings（含 SplitVocabEmbedding 的扩展行）
  ├─ 6. 若 tie_word_embeddings：重新绑定权重（to_empty 破坏了共享）
  ├─ 7. 逐 block：init_attention → init_feed_forward / init_feed_forward_moe / init_moe_v2
  ├─ 8. 若 lm_head 存在且未 tie：init_final_w_out(lm_head.w_out)
  └─ 9. 若配置了 completedp：apply_completedp_init_（见 u10-l8）
```

在真实训练流程中，`init_weights` 的调用时机很讲究：模型先在 `meta` 设备上「占位」构建，再套上 FSDP/DDP 等并行包装，**最后**才物化并初始化参数——见 [common.py:147-156](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L147-L156)，训练模块把 `max_seq_len`、`world_mesh`、`model_part_idx` 一并传入，用于同时预热 RoPE/MoE 缓存和做流水线 seed 偏移。

#### 4.1.3 源码精读

配置入口——[config.py:354-357](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L354-L357) 定义了四个初始化字段：`init_method`（默认 `InitMethod.normal`）、`init_seed`（默认 0）、`init_std`（默认 0.02）、`embedding_init_std`（默认 `None`，即回退用 `init_std`）。`build()` 再把它们原样传给 `Transformer` 构造函数（[config.py:435-439](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L435-L439)）。

总调度入口——[model.py:336-371](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L336-L371)：

```python
@torch.no_grad()
def init_weights(self, *, max_seq_len=None, max_local_microbatch_size=None,
                 device=None, world_mesh=None, model_part_idx=0) -> torch.Generator:
    device = device or self.device
    self.to_empty(device=device)
    for module in self.modules():
        if hasattr(module, "reset_parameters"):
            module.reset_parameters()
    seed = self.init_seed
    if world_mesh is not None and self.pp_enabled:
        pp_mesh = get_pp_mesh(world_mesh)
        seed += pp_mesh.get_local_rank() + model_part_idx * pp_mesh.size()
    generator = torch.Generator(device).manual_seed(seed)
```

这段代码先 `to_empty`（配合 `meta` 设备占位的两段式构建），再给无参模块走 PyTorch 默认的 `reset_parameters`，最后用（可能偏移过的）seed 建立本模型专属的 `torch.Generator`。

embedding 的初始化分发——[model.py:373-403](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L373-L403)：先算 `embedding_std = embedding_init_std if 不为 None else init_std`，调用 `init_method.init_embeddings(...)`；若词表是拆分的（`SplitVocabEmbedding`，多模态模型给词表追加的图像 token 行），额外调用 `init_method.init_embedding_weight` 初始化 `extra_weight`，保证**新增行与基础行同尺度**。

block 循环与收尾——[model.py:409-461](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L409-L461) 逐层调用 `init_attention`、`init_feed_forward`、`init_feed_forward_moe`、`init_moe_v2`；[model.py:472-483](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L472-L483) 最后初始化 LM head 的 `w_out`，并在配置了 CompletedP 时追加 `apply_completedp_init_`。

分发机制——`InitMethod.init_attention` 并不自己算标准差，而是把决策权下放给序列混合器（[init.py:237-254](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L237-L254)）：

```python
def init_attention(self, m, *, d_model, block_idx, num_blocks, std=0.02, generator=None):
    m.init_weights(init_method=self, d_model=d_model, block_idx=block_idx,
                   num_blocks=num_blocks, std=std, generator=generator)
```

这让 GatedDeltaNet、KDA 这类「非标准注意力」可以按自己的参数清单解释同一个 `InitMethod`——这是本讲 4.3 节 nanochat 能覆盖多种组件的机制基础。

分布式一致性——所有初始化函数不直接操作张量，而是经过 `_apply_init`（[init.py:16-27](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L16-L27)）：普通张量直接初始化；DTensor 则先在本地生成同形状的全量张量并初始化，再按目标分片方式 `distribute_like`，把对应分片拷贝过去。这保证了张量并行下各 rank 的分片拼起来等于单机初始化的结果。

#### 4.1.4 代码实践：跟踪一次初始化调用链

1. **实践目标**：亲手走通「Config 字段 → `init_weights()` → 某个具体权重被填充」的链路，确认 `build()` 之后权重仍是占位值。
2. **操作步骤**（以下为示例代码，非项目原有）：

   ```python
   # init_trace.py —— 示例代码
   import torch
   from olmo_core.nn.feed_forward import FeedForwardConfig
   from olmo_core.nn.transformer import TransformerConfig
   from olmo_core.nn.transformer.init import InitMethod

   config = TransformerConfig.llama_like(
       d_model=128, vocab_size=1000, n_layers=2, n_heads=4,
       feed_forward=FeedForwardConfig(hidden_size=512, bias=False),
       init_method=InitMethod.normal, init_seed=42, init_std=0.02,
   )
   model = config.build(init_device="cpu")
   w_q = model.blocks["0"].attention.w_q.weight
   print("init_weights 之前:", w_q.abs().sum().item())   # 未初始化的空存储
   model.init_weights(device=torch.device("cpu"))
   print("init_weights 之后:", w_q.std().item())          # 应接近 0.02
   ```

3. **需要观察的现象**：调用前 `abs().sum()` 是无意义的垃圾值（`to_empty` 分配的未初始化内存）；调用后 `w_q.std()` 约等于 `init_std`（截断正态的标准差）。
4. **预期结果**：第二次打印值落在 0.02 附近（采样误差约 ±10% 以内）。
5. 本环境未执行该脚本，运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`embedding_init_std` 与 `init_std` 是什么关系？

**答案**：`embedding_init_std` 是 embedding 的专属标准差覆盖项；为 `None` 时回退用 `init_std`（见 [model.py:373-376](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L373-L376)）。它只对把 `std` 传进来的初始化方法（如 `normal`）生效；`nanochat` 等按形状定标准差的方法会忽略它。

**练习 2**：为什么训练模块先套 FSDP 再调用 `init_weights`，而不是初始化完再分片？

**答案**：模型在 `meta` 设备上构建时没有真实存储，FSDP 包装时按分片策略重新规划参数布局；`init_weights` 内部通过 `_apply_init` + `distribute_like` 直接初始化 DTensor 分片（[init.py:16-27](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L16-L27)），既省去「先全量初始化再分片」的显存峰值，又保证分片结果与单机一致（[common.py:147-156](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/common.py#L147-L156)）。

**练习 3**：`init_weights` 中第 6 步为什么要「重新绑定权重」？

**答案**：`tie_word_embeddings=True` 时 LM head 与 embedding 共享同一张量，但 `to_empty` 给所有参数分配了新存储、破坏了共享，所以必须重新 `_tie_weights()`（[model.py:405-407](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L405-L407)）。

### 4.2 InitMethod 家族：从 normal 到 fan_in 的标准差设计

#### 4.2.1 概念说明

`InitMethod` 是一个 `StrEnum`（因此可以直接写进 YAML 配置），共六个成员。它们的差异可以归结为两个问题：

1. **基准标准差是多少？** 固定常数（`normal` 的 0.02）、按模型宽度（`normalized`/`fan_in`）、还是按每个矩阵自己的形状（`fan_in`/`nanochat`）？
2. **要不要深度缩放？** 残差流每经过一个 block 就多一次加法，深模型若不把「输出侧」投影的初始权重调小，激活方差会随深度线性增长。`llama` 家族用 \(1/\sqrt{2 \cdot N}\)（\(N\) 为总层数）处理这个问题。

#### 4.2.2 核心流程

各方法对同一类参数使用的标准差（`σ₀ = init_std`，`d = d_model`，`h = hidden_size`，`N = n_layers`，`i = block_idx`）：

| 参数 | normal | llama | llama_depth | normalized | fan_in | nanochat |
| --- | --- | --- | --- | --- | --- | --- |
| embedding | `σ₀` | 1.0 | 1.0 | `d^{-½}` | 1.0 | 1.0 |
| 注意力 w_q/w_k/w_v | `σ₀` | `σ₀` | `σ₀` | `d^{-½}` | `fan_in^{-½}` | 谱条件公式 |
| 注意力 w_out | `σ₀` | `σ₀/√(2N)` | `σ₀/√(2(i+1))` | `d^{-½}/√(2N)` | `fan_in^{-½}` | 谱条件公式 |
| 前馈 w1 | `σ₀` | `σ₀` | `σ₀` | `d^{-½}` | `fan_in^{-½}` | 谱条件公式 |
| 前馈 w3 | `σ₀` | `σ₀/√(2N)` | `σ₀/√(2(i+1))` | `d^{-½}` | `fan_in^{-½}` | 谱条件公式 |
| 前馈 w2 | `σ₀` | `σ₀/√(2N)`（继承 w3 的缩放） | 同左（按 `i`） | `d^{-½}/√(2N)` | `h^{-½}` | 谱条件公式 |
| LM head w_out | `σ₀` | `d^{-½}` | `d^{-½}` | `d^{-½}` | `d^{-½}` | 谱条件公式 |

深度缩放的数学直觉：若每个 block 对残差流贡献的扰动方差为 \(v\)，经过 \(N\) 层后方差约为 \(N \cdot v\)；把输出侧投影缩小 \(\sqrt{2N}\) 倍（注意力与 MLP 各计一次「乘 2」）即可让初始激活方差与深度无关——\(\text{Var} \approx \sum_{i=1}^{N} \frac{v}{2N} \cdot 2 = v\)。

`normalized`（nGPT）比较特殊：初始化之后 `NormalizedTransformer` 还会把每个矩阵重新归一化到单位球面（见 [model.py:1379-1383](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1379-L1383)），初始标准差只决定球面上的采样方向密度。

#### 4.2.3 源码精读

枚举定义与官方文档——[init.py:123-171](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L123-L171)：六个成员各自的 docstring 说明了设计意图，其中 `fan_in` 明确「每个矩阵用 \(\mathrm{std}=1/\sqrt{d_{in}}\)、embedding 用 1.0、无深度缩放」。

最底层的工具函数——[init.py:30-43](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L30-L43) 的 `init_linear` 把 `nn.Linear` / `nn.Conv1d` 的权重按给定 `std` 做截断正态初始化（截断在 ±3σ），bias 置零。全库的线性层初始化最终都汇到它。

前馈的分支逻辑——[init.py:256-298](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L256-L298)：注意 `std` 变量在 w1→w3→w2 之间是**顺序传递**的——`llama` 在 w3 处把 `std` 除以 \(\sqrt{2N}\) 后，w2 分支没有重置，于是 w2 也使用缩小后的值。读这段代码时要顺着变量赋值走。

注意力的落地——[attention/__init__.py:1275-1310](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1275-L1310)：`fan_in` 分支对 w_q/w_k/w_v/w_out 各自用 `in_features**-0.5`（GQA 下这些矩阵的输入维相同）；`llama`/`llama_depth` 只缩放 `w_out`；attention sink（若启用）跟随 `w_out` 的标准差。

embedding 的落地——[init.py:186-216](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L186-L216)：`llama`/`llama_depth`/`nanochat` 走 `nn.init.normal_`（默认 std=1.0）；`normalized` 用 `d_model**-0.5`；`fan_in` 用 1.0（再除以 `embed_scale`）；其余（`normal`）用调用方传入的 `std` 做截断正态。

LM head 的落地——[init.py:218-235](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L218-L235)：`init_final_w_out` 中 `llama`/`llama_depth`/`normalized`/`fan_in` 统一用 `d_model**-0.5`——输出层是「读出」矩阵，宽度越大权重越小。

官方测试的断言可以当「规格说明书」读——[init_test.py:44-92](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L44-L92) 的 `test_fan_in_init_dense` 精确断言了 embedding std≈1.0、注意力四个投影 std≈`1/√d_model`、前馈 w1/w3≈`1/√d_model`、w2≈`1/√hidden_size`、LM head≈`1/√d_model`。

#### 4.2.4 代码实践：修改初始化配置，观察参数分布

1. **实践目标**：直观感受 `init_method` 与 `init_std` 对权重分布的影响。
2. **操作步骤**（示例代码）：

   ```python
   # init_compare.py —— 示例代码
   import torch
   from olmo_core.nn.feed_forward import FeedForwardConfig
   from olmo_core.nn.transformer import TransformerConfig
   from olmo_core.nn.transformer.init import InitMethod

   def stats(init_method, init_std=0.02):
       model = TransformerConfig.llama_like(
           d_model=128, vocab_size=1000, n_layers=4, n_heads=4,
           feed_forward=FeedForwardConfig(hidden_size=512, bias=False),
           init_method=init_method, init_seed=42, init_std=init_std,
       ).build(init_device="cpu")
       model.init_weights(device=torch.device("cpu"))
       b0 = model.blocks["0"]; b3 = model.blocks["3"]
       for tag, w in [
           ("emb      ", model.embeddings.weight),
           ("L0 attn w_out", b0.attention.w_out.weight),
           ("L3 attn w_out", b3.attention.w_out.weight),
           ("L0 ff   w2   ", b0.feed_forward.w2.weight),
           ("lm_head     ", model.lm_head.w_out.weight),
       ]:
           print(f"{init_method.value:11s} {tag} mean={w.mean():+.4f} std={w.std():.4f}")

   stats(InitMethod.normal)
   stats(InitMethod.normal, init_std=0.05)   # 只改基准标准差
   stats(InitMethod.llama)                    # 深度缩放
   stats(InitMethod.fan_in)
   ```

3. **需要观察的现象**：
   - `normal` → `normal(0.05)`：所有 `std=0.02` 的位置整体变大 2.5 倍，均值始终在 0 附近；
   - `normal` → `llama`：embedding 的 std 从 0.02 变为约 1.0，LM head 变为约 `1/√128 ≈ 0.088`；
   - `llama` 与 `fan_in`：注意 `w_out` 的 std 与层数（`llama`）或逐层（`llama_depth`）的关系。
4. **预期结果**：与 4.2.2 表格逐项吻合；截断正态的均值接近 0。
5. 运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`llama` 与 `llama_depth` 的区别是什么？各自适合什么场景？

**答案**：两者都把「输出侧」投影（注意力 `w_out`、前馈 `w3`/`w2`）按 \(1/\sqrt{2N}\) 缩小；`llama` 用总层数 `num_blocks`（全模型统一），`llama_depth` 用 `block_idx + 1`（越深越小）。`llama_depth` 是逐层变体，让深层 block 的初始扰动更小（见 [init.py:284-287](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L284-L287) 与 [attention/__init__.py:1300-1303](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1300-L1303)）。

**练习 2**：`fan_in` 方法下，一个 `in_features=512`、`out_features=128` 的投影矩阵标准差是多少？同一个矩阵换成 `out_features=2048` 呢？

**答案**：`fan_in` 只看输入维：两种情况都是 `512**-0.5 ≈ 0.0442`。对比 `nanochat`：后者还要乘 `min(1, √(fan_out/fan_in))`，收缩矩阵（128 输出）会再除以 2，扩张矩阵（2048 输出）不变——这正是下一节的主题。

**练习 3**：为什么 `fan_in` 不支持 `GatedDeltaNet`？

**答案**：[recurrent.py:257-260](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/recurrent.py#L257-L260) 对 `fan_in` 显式抛出 `NotImplementedError`——这类混合器的特殊参数（如 `A_log`、`dt_bias`）有自己的初始化逻辑，与逐矩阵 fan-in 规则的组合尚未实现。

### 4.3 InitMethod.nanochat：谱条件初始化

#### 4.3.1 概念说明

`nanochat` 是本轮提交（704625ad9）引入的初始化方法，名字来自 Karpathy 的 [nanochat](https://github.com/karpathy/nanochat) 项目在 2026 年 1 月前使用的方案，其理论依据是特征学习的**谱条件**（spectral condition，[Yang et al., 2023](https://arxiv.org/abs/2310)）。核心公式——每个把 `fan_in` 个特征映射到 `fan_out` 个特征的权重矩阵，从截断正态分布采样，标准差为：

\[
\mathrm{std} = \frac{1}{\sqrt{\mathrm{fan\_in}}} \cdot \min\left(1,\ \sqrt{\frac{\mathrm{fan\_out}}{\mathrm{fan\_in}}}\right)
\]

分三种情形理解：

| 情形 | 条件 | 化简后的 std | 直觉 |
| --- | --- | --- | --- |
| 方阵 | `fan_out = fan_in` | \(1/\sqrt{\mathrm{fan\_in}}\) | μP 隐藏层尺度 |
| 扩张 | `fan_out > fan_in` | \(1/\sqrt{\mathrm{fan\_in}}\)（min 取 1） | 输出方差 \(\mathrm{fan\_in}\cdot\mathrm{std}^2 = 1\)，规模不随宽度漂移，同时谱范数随宽度增长，保留特征学习能力 |
| 收缩 | `fan_out < fan_in` | \(\sqrt{\mathrm{fan\_out}}/\mathrm{fan\_in}\) | 比 \(1/\sqrt{\mathrm{fan\_in}}\) 再乘 \(\sqrt{\mathrm{fan\_out}/\mathrm{fan\_in}}\) 收缩矩阵的尺度打折 |

与 `fan_in` 方法相比，`nanochat` 唯一的差别就是多了 `min(1, √(fan_out/fan_in))` 因子——只影响收缩矩阵（如 GQA 下输出维较小的 w_k/w_v、`vocab_size < d_model` 时的 LM head）。

它的其他约定（据官方 docstring，[init.py:156-171](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L156-L171)）：

- **无深度缩放**：与 `llama` 的 \(1/\sqrt{2N}\) 不同，所有层用同一规则；
- **忽略 `init_std` 与 `embedding_init_std`**：标准差完全由矩阵形状决定；
- **embedding 用 std = 1.0 的普通正态**（与 `llama` 一致）；
- 与 nanochat 原版不同，**输出投影与 LM head 不置零**（保持可训练的对称起点）；
- **不支持 normalized transformer**——因为后者初始化后会把矩阵归一化，抹掉按 fan_in/fan_out 设计的尺度（[model.py:1350-1355](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L1350-L1355) 显式报错）。

#### 4.3.2 核心流程

nanochat 覆盖的组件及其入口：

```text
Transformer.init_weights()
  ├─ embeddings ──────────── init_embedding_weight：normal_(std=1.0)（含 SplitVocabEmbedding 扩展行）
  ├─ 每个 block：
  │   ├─ init_attention ──┬─ Attention（非融合）: w_q/w_k/w_v/w_g/w_out → init_linear_nanochat
  │   │                   │    sinks → normal_(std=nanochat_std(w_out))
  │   │                   ├─ FusedAttentionV2: 融合 w_qkv → init_fused_linear_nanochat（Q/K/V 分块取 std）
  │   │                   │    w_g → init_linear_nanochat；w_out → init_linear_nanochat
  │   │                   ├─ GatedDeltaNet (recurrent.py): w_q/w_k/w_v/w_a/w_b/w_g/q·k·v_conv1d → init_linear_nanochat
  │   │                   │    w_out → init_linear_nanochat（A_log/dt_bias 保持原有特殊初始化）
  │   │                   └─ KimiDeltaAttention (kda.py): 全部线性投影 → init_linear_nanochat
  │   ├─ init_feed_forward ── w1/w3/w2 → init_linear_nanochat
  │   ├─ init_feed_forward_moe ── router → trunc_normal(nanochat_std(d_model, num_experts))
  │   │                        experts w1/w2/w3、latent 投影、shared_mlp → nanochat 规则
  │   └─ init_moe_v2 ── 融合 MoE：w_up_gate/w_down 分别按 (d_model→hidden)/(hidden→d_model) 取 std
  └─ lm_head.w_out ──────── init_final_w_out：std = nanochat_std(in, out)
```

两个特殊形状的处理值得注意：

- **融合矩阵**（如把 Q/K/V 三个投影沿输出维拼成一个 `w_qkv`）：三个投影的 `fan_out` 不同（GQA 下 K/V 更窄），必须沿输出维切块、每块单独按自己的 `nanochat_std` 初始化——这是 `init_fused_linear_nanochat` 存在的原因。
- **深度卷积**（`q_conv1d` 等）：depthwise 卷积把每个通道从 `kernel_size` 个时间步映射到自身，等价于 `fan_in = fan_out = kernel_size` 的方阵，故 std = \(1/\sqrt{\mathrm{kernel\_size}}\)。

#### 4.3.3 源码精读

**公式本体**——[init.py:58-68](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L58-L68)：

```python
def nanochat_std(fan_in: int, fan_out: int) -> float:
    return fan_in**-0.5 * min(1.0, (fan_out / fan_in) ** 0.5)
```

一行实现谱条件公式，独立成函数方便测试与复用。

**单矩阵初始化**——[init.py:71-91](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L71-L91) 的 `init_linear_nanochat`：`nn.Linear` 用 `nanochat_std(in_features, out_features)`；`nn.Conv1d` 先断言是 depthwise（`groups == in_channels == out_channels`）再按 kernel_size 取 std，最后复用 `init_linear` 做截断正态。

**融合矩阵初始化**——[init.py:94-120](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L94-L120) 的 `init_fused_linear_nanochat`：先校验各投影输出维之和等于矩阵总输出维，然后 `w.split(out_features, dim=0)` 逐块用 `nanochat_std(m.in_features, rows.shape[0])` 初始化。

**前馈**——[init.py:266-269](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L266-L269)：nanochat 分支对 `w1`、`w3`、`w2` 统一调 `init_linear_nanochat` 后直接 `return`，完全绕开基于 `init_std` 的常规逻辑。

**注意力（非融合）**——[attention/__init__.py:1262-1273](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1262-L1273)：`w_q/w_k/w_v/w_g/w_out` 全走 `init_linear_nanochat`；attention sink 用 `nanochat_std(w_out.in_features, w_out.out_features)` 的普通正态。

**融合注意力**——[attention/__init__.py:1838-1873](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1838-L1873)：`FusedAttentionV2.init_weights` 的 nanochat 分支把 `w_qkv` 交给 `init_fused_linear_nanochat`，其余投影走 `init_linear_nanochat`。

**GatedDeltaNet**——[recurrent.py:276-294](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/recurrent.py#L276-L294)：七个线性投影加三个 depthwise 卷积全部 `init_linear_nanochat`；`A_log`/`dt_bias` 这类状态参数保持自己的特殊初始化（与 Mamba 传统一致）；`w_out` 在 nanochat 分支同样按形状初始化并提前返回。

**KDA**——[kda.py:363-375](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/kda.py#L363-L375)：所有线性投影换 `init_linear_nanochat`，`w_out` 单独处理。

**MoE**——[init.py:312-339](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L312-L339)：router 权重按 `nanochat_std(d_model, num_experts)`（把「选专家」看成一个 `fan_out = num_experts` 的矩阵）；专家 MLP 的 w1/w3 用 `nanochat_std(d_model, hidden_size)`，w2 用 `nanochat_std(hidden_size, d_model)`；latent 投影与 shared MLP 分别处理。融合 MoE v2 版本在 [init.py:442-488](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L442-L488)，`w_up_gate`（gate+up 融合）按 `(d_model, hidden_size)` 取 std，`w_down` 按 `(hidden_size, d_model)`，路由专家权重改用 `ep_generator`（专家并行分片的随机流）。

**测试即规格**——[init_test.py:270-280](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L270-L280) 用四组参数化用例锁定公式（方阵 256→256 得 1/16、扩张 256→1024 得 1/16、收缩 1024→256 得 1/64）；[init_test.py:291-323](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L291-L323) 的 `test_nanochat_init_dense` 故意把 `vocab_size` 设成 128（小于 d_model），验证 LM head 作为收缩矩阵也严格符合公式，并断言 embedding std≈1.0、`init_std=0.5` 被忽略；[init_test.py:420-459](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L420-L459) 覆盖 KDA 与 GatedDeltaNet（含 depthwise 卷积 std = `kernel_size**-0.5`）。

#### 4.3.4 代码实践：normal vs nanochat，各层标准差与 fan_in/fan_out 的关系

1. **实践目标**：验证同一模型在两种初始化下，各层权重的实际 std 与理论预测一一对应。
2. **操作步骤**（示例代码）：

   ```python
   # nanochat_check.py —— 示例代码
   import torch
   from olmo_core.nn.feed_forward import FeedForwardConfig
   from olmo_core.nn.transformer import TransformerConfig
   from olmo_core.nn.transformer.init import InitMethod, nanochat_std

   def build(init_method):
       return TransformerConfig.llama_like(
           d_model=128, vocab_size=1000, n_layers=2, n_heads=4,
           feed_forward=FeedForwardConfig(hidden_size=512, bias=False),
           init_method=init_method, init_seed=42, init_std=0.02,
       ).build(init_device="cpu")

   for method in (InitMethod.normal, InitMethod.nanochat):
       m = build(method)
       m.init_weights(device=torch.device("cpu"))
       att, ff = m.blocks["0"].attention, m.blocks["0"].feed_forward
       print(f"--- {method.value} ---")
       for tag, w in [
           ("embeddings", m.embeddings.weight),
           ("attn.w_q  (128 ->128)", att.w_q.weight),
           ("attn.w_out(128 ->128)", att.w_out.weight),
           ("ff.w1     (128 ->512)", ff.w1.weight),
           ("ff.w2     (512 ->128)", ff.w2.weight),
           ("lm_head   (128 ->1000)", m.lm_head.w_out.weight),
       ]:
           pred = nanochat_std(w.shape[1], w.shape[0]) if method == InitMethod.nanochat else 0.02
           print(f"{tag}: 实测 std={w.std():.4f}  预测={pred:.4f}")

   # 手算对照（d_model=128, hidden=512, vocab=1000）：
   #   ff.w1  扩张: 1/√128            ≈ 0.0884
   #   ff.w2  收缩: √128/512          ≈ 0.0221
   #   lm_head 扩张: 1/√128           ≈ 0.0884
   #   normal: 一律 0.02（embedding 亦为 0.02；nanochat 的 embedding 为 1.0）
   ```

3. **需要观察的现象**：`normal` 下所有线性层 std 都在 0.02 附近、embedding 也是 0.02；`nanochat` 下各层 std 随形状变化——扩张矩阵 0.0884、收缩矩阵 0.0221、embedding 约 1.0。
4. **预期结果**：nanochat 列的「实测」与「预测」逐项吻合（相对误差 10% 以内，截断正态的采样噪声所致）。注意 `nn.Linear.weight` 形状是 `(out_features, in_features)`，所以 `fan_in = shape[1]`、`fan_out = shape[0]`。
5. 运行结果**待本地验证**（也可直接运行官方测试 `pytest -v src/test/nn/transformer/init_test.py -k nanochat` 代替）。

#### 4.3.5 小练习与答案

**练习 1**：`nanochat_std(256, 1024)` 和 `nanochat_std(1024, 256)` 各是多少？

**答案**：扩张 `min(1, √4)=1`，得 `256**-0.5 = 1/16 = 0.0625`；收缩 `min(1, √0.25)=0.5`，得 `1024**-0.5 · 0.5 = 1/64 ≈ 0.0156`。与官方测试参数完全一致（[init_test.py:270-280](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L270-L280)）。

**练习 2**：为什么 nanochat 要忽略 `init_std`？

**答案**：谱条件公式的标准差由矩阵形状唯一决定，任何全局常数都会破坏「输出方差 = 1（扩张）/按比例收缩」的性质；宽度迁移（muP 思想）也要求超参不随宽度手工调。官方 docstring 明确写了「The base init_std and embedding_init_std are ignored」（[init.py:169-170](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L169-L170)）。

**练习 3**：融合的 `w_qkv` 为什么不能直接调用 `init_linear_nanochat`？

**答案**：GQA 下 Q 的输出维是 K/V 的数倍，融合矩阵作为整体只有一组 `in/out_features`，直接初始化会让 Q、K、V 共用一个 std。`init_fused_linear_nanochat` 按各投影的输出维切块分别取 std（[init.py:94-120](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L94-L120)），官方测试也按切块逐段断言（[init_test.py:351-373](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L351-L373)）。

### 4.4 可复现性：init_seed、torch.Generator 与 seed_all

#### 4.4.1 概念说明

「可复现」在训练库里有两层含义，OLMo-core 用两个机制分别覆盖：

1. **权重层面的确定性**：`init_seed` → 专属 `torch.Generator`。同一份 Config、同一个 `init_seed`，无论何时何地构建，权重逐比特一致；它与全局随机状态无关，所以数据加载的随机增强不会改变初始权重。流水线并行下还会按 `pp_rank` 和 `model_part_idx` 偏移 seed，保证交错式流水线的不同模型分片不拿到同一随机流。
2. **全局 RNG 的确定性**：`seed_all(seed)` 同时播种 Python `random`、NumPy、PyTorch CPU/CUDA 的全局随机源，覆盖 dropout、数据打乱、采样等一切「非权重」随机性。

#### 4.4.2 核心流程

```text
可复现训练的随机源划分
  ├─ init_seed ──→ torch.Generator(device).manual_seed(init_seed [+ PP 偏移])
  │                 └─ 只被 init_weights 传入的初始化函数消耗
  └─ seed_all(seed) ─→ random / numpy / torch(CPU+CUDA) 全局状态
                      └─ 被 dropout、DataLoader、采样器等消耗
```

PP 偏移公式（[model.py:366-371](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L366-L371)）：

\[
\mathrm{seed} = \mathrm{init\_seed} + \mathrm{pp\_local\_rank} + \mathrm{model\_part\_idx} \times \mathrm{pp\_size}
\]

#### 4.4.3 源码精读

seed 的推导——见上文引用的 [model.py:366-371](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L366-L371)：仅当 `world_mesh is not None` 且启用 PP 时偏移；不开 PP 时 `model_part_idx` 被完全忽略，保证既有非 PP 训练的随机流与历史版本一致。

`seed_all`——[utils.py:209-224](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/utils.py#L209-L224)：校验 seed 在 \([0, 2^{32}-1]\) 内，依次调用 `random.seed`、`np.random.seed`、`torch.manual_seed`、`torch.cuda.manual_seed_all`。注释特别说明 `torch.manual_seed` 本身可能已级联播种 CUDA，再显式调一次是为了保险。

generator 的传递——`init_weights` 建好的 generator 一路传给每个 `init_*` 函数，最终交给 `nn.init.trunc_normal_(..., generator=generator)`（如 [init.py:30-43](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L30-L43)）。融合 MoE v2 额外接受 `ep_generator`：路由专家权重可能被专家并行切分到不同 rank，用独立随机流保证分片一致（[init.py:410-427](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/init.py#L410-L427)）。

确定性测试——[init_test.py:511-576](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L511-L576) 用三个测试锁定行为：(a) 交错流水线下不同 `model_part_idx` 的两个分片权重**必须不同**；(b) 相同 `(pp_rank, model_part_idx)` 的两次构建**必须逐比特相同**；(c) 未启用 PP 时 `model_part_idx` 不影响结果。测试辅助函数 `_build_tiny_model`（[init_test.py:500-508](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L500-L508)）就是一个「同 seed 两次构建」的最小范例。

#### 4.4.4 代码实践：同 seed 构建两次，验证权重完全一致

1. **实践目标**：验证 (a) `init_seed` 决定权重、与全局随机状态无关；(b) `seed_all` 控制的是另一条随机流。
2. **操作步骤**（示例代码）：

   ```python
   # determinism_check.py —— 示例代码
   import torch
   from olmo_core.utils import seed_all
   from olmo_core.nn.feed_forward import FeedForwardConfig
   from olmo_core.nn.transformer import TransformerConfig

   def build(init_seed):
       m = TransformerConfig.llama_like(
           d_model=64, vocab_size=128, n_layers=2, n_heads=2,
           feed_forward=FeedForwardConfig(hidden_size=128, bias=False),
           init_seed=init_seed,
       ).build(init_device="meta")
       m.init_weights(device=torch.device("cpu"))
       return m

   # (a) 不动全局 RNG，两次构建权重应逐比特一致
   a, b = build(42), build(42)
   assert torch.equal(a.blocks["0"].attention.w_q.weight, b.blocks["0"].attention.w_q.weight)
   assert torch.equal(a.embeddings.weight, b.embeddings.weight)

   # (b) 中间插入全局随机操作，仍不影响 init_seed=42 的权重
   _ = torch.randn(1_000_000)          # 消耗全局随机流
   c = build(42)
   assert torch.equal(a.blocks["0"].attention.w_q.weight, c.blocks["0"].attention.w_q.weight)

   # (c) 换 init_seed 则权重不同
   d = build(43)
   assert not torch.equal(a.blocks["0"].attention.w_q.weight, d.blocks["0"].attention.w_q.weight)

   # (d) seed_all 影响的是全局流，而不是 init_seed 流
   seed_all(1234)
   x1 = torch.randn(3)
   seed_all(1234)
   x2 = torch.randn(3)
   assert torch.equal(x1, x2)          # 全局流可复现
   e = build(42)
   assert torch.equal(a.embeddings.weight, e.embeddings.weight)  # 权重不受影响
   print("全部断言通过")
   ```

3. **需要观察的现象**：所有 `assert` 通过；把 (a) 中两个 `build(42)` 改成不同 seed 后 `torch.equal` 返回 False。
4. **预期结果**：确定性断言全部成立——这正是官方测试 [init_test.py:538-558](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L538-L558) 的断言内容。
5. 运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`init_seed` 与 `seed_all` 的分工是什么？

**答案**：`init_seed` 只喂给 `init_weights` 内部创建的专属 `torch.Generator`，决定初始权重；`seed_all` 播种 Python/NumPy/PyTorch 全局 RNG，决定 dropout、数据顺序等其他随机性。两条流互相独立（[utils.py:209-224](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/utils.py#L209-L224)、[model.py:371](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L371)）。

**练习 2**：交错式流水线并行下，为什么同一 rank 上的多个模型分片必须拿不同的 seed？

**答案**：否则两个分片用同一个 generator 流初始化，对应层的权重会完全相同，等于同一子网络训练了两份。偏移公式 `seed += pp_local_rank + model_part_idx × pp_size` 保证 `(pp_rank, model_part_idx)` 二元组到 seed 的映射无碰撞。

**练习 3**：想为新参数类型自定义初始化，应该在哪里动手？

**答案**：优先在组件层实现——序列混合器实现 `init_weights(init_method=...)` 并在自己的分支里处理新参数（参照 [recurrent.py:241-302](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/recurrent.py#L241-L302) 的 GatedDeltaNet 模式）；若要新增一种全局方法，则在 `InitMethod` 枚举加成员并在各 `init_*` 分发函数中补分支（注意同时更新 `init_embedding_weight`、`init_final_w_out`、attention 与前馈四处，参照 nanochat 的提交）。不支持组合时显式 `raise NotImplementedError`（如 [init_test.py:482-485](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/transformer/init_test.py#L482-L485) 检验的行为）。

## 5. 综合实践

**任务：给你的迷你模型出具一份「初始化体检报告」。**

把本讲三个实践合并成一个脚本，对 `d_model=256, hidden_size=768, vocab_size=512, n_layers=4, n_heads=8, n_kv_heads=2`（GQA）的 `llama_like` 模型：

1. 对 `InitMethod.normal`、`InitMethod.fan_in`、`InitMethod.nanochat` 各构建一次（固定 `init_seed=7`），打印一张表：每行是参数名（embedding、L0 的 w_q/w_k/w_v/w_out、w1/w2/w3、lm_head.w_out），列是「实际 std / 理论 std」。
2. 对每个方法验证确定性：同 seed 重建一次，`torch.equal` 逐参数比对。
3. 回答两个问题（写进报告注释）：
   - GQA 下 `w_k` 的形状是 `(64, 256)`（8 头中 2 个 KV 头、head_dim=32 时为示例值，以实际构建为准）——它是收缩矩阵，`nanochat` 与 `fan_in` 给它的 std 相差几倍？（提示：比值恰为 \(\sqrt{\mathrm{fan\_out}/\mathrm{fan\_in}}\)）
   - 把 `vocab_size` 从 512 改成 4096，`nanochat` 下 lm_head 的 std 如何变化？`normal` 下呢？

验收标准：理论列全部能对上实测（±10%）；`torch.equal` 全部为 True；两个问题的答案能分别解释 `min(1, √(fan_out/fan_in))` 的两个分支。若不想写脚本，运行 `pytest -v src/test/nn/transformer/init_test.py` 并逐条对应本讲公式也可视为完成。

## 6. 本讲小结

- 初始化由 `TransformerConfig` 的 `init_method`/`init_seed`/`init_std`/`embedding_init_std` 四字段驱动，`build()` 不填权重，必须显式调用 `model.init_weights()`（训练流程中由训练模块在 FSDP/DDP 包装之后调用）。
- `init_weights()` 按「embedding → 逐 block（注意力/前馈/MoE）→ LM head」的顺序分发，决策权下放给各组件的 `init_weights`，所以同一个 `InitMethod` 能覆盖标准注意力、KDA、GatedDeltaNet 等不同参数清单。
- 六种方法的本质差异是「基准 std 怎么定」与「是否深度缩放」：`normal` 常数、`llama`/`llama_depth` 输出侧乘 \(1/\sqrt{2N}\)、`fan_in` 逐矩阵 \(1/\sqrt{\mathrm{fan\_in}}\)、`nanochat` 谱条件、`normalized` 配合初始化后归一化。
- `nanochat` 的公式 \(\mathrm{std}=\frac{1}{\sqrt{\mathrm{fan\_in}}}\min(1,\sqrt{\mathrm{fan\_out}/\mathrm{fan\_in}})\)：扩张/方阵矩阵退化为 `fan_in` 方法，收缩矩阵额外打折；embedding 用 std=1.0、忽略 `init_std`、无深度缩放、不支持 normalized transformer。
- 融合矩阵（w_qkv、w_up_gate）与 depthwise 卷积是两类特殊形状，分别由 `init_fused_linear_nanochat`（按输出维切块）和 `fan_in = fan_out = kernel_size` 的约定处理。
- 可复现性双轨制：`init_seed` 走专属 `torch.Generator`（PP 下按 `pp_rank + part_idx × pp_size` 偏移），`seed_all()` 播种全局 RNG；`_apply_init` + `distribute_like` 保证 DTensor 分片与单机初始化一致。

## 7. 下一步学习建议

- **u5-l1（优化器配置体系）**：初始化决定了起点，优化器超参决定轨迹；下一单元会看到参数如何按组划分——那是理解 u10-l7 的前置。
- **u10-l7（muP 学习率与 NanoChat 初始化）**：nanochat 初始化正是为搭配 muP 学习率设计的，`base_width`、`named_param_shapes` 等机制会把「按形状初始化」推进为「按形状调学习率」，学完本讲再看会非常自然。
- **u10-l8（CompletedP 参数化）**：`init_weights` 最后一步的 `apply_completedp_init_`（[model.py:480-481](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L480-L481)）在那里展开为宽度、深度、批量、时长的统一缩放规则。
- **源码延伸阅读**：`src/test/nn/transformer/init_test.py` 是本讲最好的「可执行规格」；提交 704625ad9 的 diff 完整展示了「新增一种 InitMethod」要动哪些文件，是想给项目贡献新初始化方法的最佳模板。
