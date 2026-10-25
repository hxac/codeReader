# u2-l1 模型几何常量与 config.json 探针

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 GLM5.2 的关键几何数字（78 层 / hidden 6144 / vocab 154 880 / 256+1 专家 / index_topk 2048 / 上下文上限 1 048 576），并能手推 MLA 各维度的派生关系（如 `QK_HEAD_DIM = nope 192 + rope 64 = 256`）。
2. 理解 `probe_config_json` 的 fail-fast 校验哲学：为什么本 crate 把全部几何钉死为编译期常量，再在启动期逐字段核对 `config.json`，任何一处不符立即拒绝启动。
3. 会推导 78 层中哪些是 full indexer 层（答案是 21 层），并知道这个判定在 KV 页布局和权重构建两处被真实消费。

本讲承接 u1-l3 建立的源码地图。u1-l3 的结论是：**算子层的形状全部来自 config 编译期常量**——本讲就深入这个「config」，把每一颗常量钉子钉在哪里、为什么钉、谁来核对，一次讲清。

## 2. 前置知识

- **config.json**：Hugging Face 格式模型目录下的一个 JSON 文件，描述模型的架构超参（层数、维度、专家数等）。推理引擎启动时第一步通常就是读它。GLM5.2 的 `model_type` 是 `glm_moe_dsa`，架构名是 `GlmMoeDsaForCausalLM`。
- **编译期常量 vs 运行时配置**：一般引擎会「读 config.json 然后照着分配内存」。PegaInfer 的 GLM5.2 走了另一条路：把几何写成 Rust `const`，让所有张量形状在编译期就是已知事实（这对 CUDA Graph 捕获、scratch 启动期预分配至关重要，见 u6）；`config.json` 则降级为「被核对的对象」——不匹配就拒绝启动，绝不将错就错。
- **MLA（Multi-head Latent Attention）维度直觉**：MLA 把每层的 KV 压缩成低秩 latent 向量缓存（本模型是 512 维压缩 + 64 维 rope 分量），查询也先压到 2048 维 lora 再展开。你暂时只需记住「维度多、但全部可由少数几个基础常量推导」。
- **fail-fast**：错误尽早暴露。启动期报 `hidden_size mismatch` 总好过服务中段产出乱码 logits。
- **serde_json::Value**：一个动态 JSON 树类型。`probe_config_json` 接收 `&Value` 而不是文件路径，所以它是纯函数——不碰文件、不碰 GPU，天然可单测（虽然 `config.rs` 本身没有测试模块，校验逻辑由 `pegainfer-server` 侧的 ModelLine 分发间接使用）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/config.rs` | 本讲主角：全部几何常量、`glm52_layer_has_full_indexer` 层类型谓词、`probe_config_json` 校验链、三个复合校验器与字段读取助手 |
| `src/lib.rs` | 把 `probe_config_json` 再导出为 crate 级接口；`validate_startup` 在 launch 链路里第二次调用它 |
| `src/model_line.rs` | `ModelLine::probe`——服务端按 `model_type` 识别模型线时的第一次调用 |
| `src/model/mod.rs` | `glm52_page_layout` 消费 full-indexer 谓词，决定 KV 页里每层是否切出 index-K 槽位 |
| `src/model/build.rs` | 权重构建时消费同一谓词，决定某层是否装载 indexer 权重 |

## 4. 核心概念与源码讲解

### 4.1 几何常量族

#### 4.1.1 概念说明

`src/config.rs` 的前 60 行是整个 crate 的「度量衡」：后面所有算子（fp8 投影、MLA、indexer、MoE）的缓冲区宽度、权重形状、kernel 参数都从这里的常量推导。把它们钉死为 `const` 带来三个好处：

1. **形状是编译期事实**：Rust 可以用常量泛型（如 u3-l3 将讲的 `Rows<C>`）把「缓冲宽度正确」变成类型检查，而不是运行时 assert。
2. **CUDA Graph 友好**：整步捕获要求所有张量形状在捕获期固定（u6-l3），常量几何是前提。
3. **防御性**：如果有人拿错 checkpoint（比如把 Qwen3 的目录指给 GLM5.2），probe 会在第一秒拒绝，而不是加载到一半才崩。

#### 4.1.2 核心流程

常量按语义分五组，派生关系如下（全部可在源码注释与定义中验证）：

```text
全局几何:   LAYERS 78 = 77 个主干层 + MTP_LAYER 78（第 78 层是投机解码层）
           DENSE_LAYERS 3   → 前 3 层是 dense MLP，之后全是 MoE
           MAX_CONTEXT 1_048_576 = 2^20（checkpoint 的 max_position_embeddings）

MLA 维度:  QK_HEAD_DIM(256) = QK_NOPE_HEAD_DIM(192) + QK_ROPE_HEAD_DIM(64)
           Q_B_OUT(16384)   = HEADS(64) × QK_HEAD_DIM(256)
           KV_A_OUT(576)    = KV_LORA_RANK(512) + QK_ROPE_HEAD_DIM(64)
           KV_B_OUT(28672)  = HEADS(64) × (NOPE(192) + V_HEAD_DIM(256))
           O_PROJ_IN(16384) = HEADS(64) × V_HEAD_DIM(256)
           SM_SCALE(0.0625) = 1/√256
           ROPE_HALF(32)    = QK_ROPE_HEAD_DIM / 2

MLP/MoE:   DENSE_INTERMEDIATE 12288（dense 层中间维）
           EXPERT_INTERMEDIATE 2048（每个专家的中间维）
           ROUTED_EXPERTS 256 + SHARED_EXPERTS 1，TOPK 8
           ROUTED_SCALING_FACTOR 2.5

indexer:   INDEX_TOPK 2048（稀疏注意力每 token 选 2048 个 KV 槽）
           INDEX_TOPK_FREQ 4 / INDEX_SKIP_TOPK_OFFSET 3（层类型节奏，见 4.3）
           INDEX_HEAD_DIM 128 × INDEX_HEADS 32
```

#### 4.1.3 源码精读

**全局几何常量**——[src/config.rs:L9-L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L9-L18) 定义 hidden/vocab/层数与上下文上限。注意 `GLM52_MTP_LAYER = GLM52_LAYERS = 78`：checkpoint 里的 MTP（multi-token prediction）解码器被当作「第 78 层」，紧接在 78 个主干层（0..77）之后；`GLM52_MAX_CONTEXT` 的注释点明它同时是 launch 期 `--max-model-len` 的架构天花板。

**MLA 维度族**——[src/config.rs:L20-L32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L20-L32)。五个派生常量全部由基础常量组合而来，例如 `GLM52_QK_HEAD_DIM` 直接写成 `GLM52_QK_NOPE_HEAD_DIM + GLM52_QK_ROPE_HEAD_DIM` 的表达式——Rust `const` 支持常量折叠，所以「派生关系」就写在代码里，不需要注释维护。`GLM52_KV_HEADS` 是 64（与查询头数相同），但 MLA 的实际 KV 缓存不按头存，而是按 latent 存，这正是 `KV_LORA_RANK=512` 的意义。

**softmax 缩放与 rope 半宽**——[src/config.rs:L34-L39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L34-L39)。注意力 softmax 的缩放因子

\[ \text{SM\_SCALE} = \frac{1}{\sqrt{\text{QK\_HEAD\_DIM}}} = \frac{1}{\sqrt{256}} = 0.0625 \]

注释同时记录了一个 config 事实：`rope_type "default"` 意味着没有 YaRN mscale 修正，所以缩放就是裸的 \(1/\sqrt{256}\)。这个 f32 值是 GPU norm/attention kernel 直接消费的。

**MLP 与 MoE 常量**——[src/config.rs:L41-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L41-L50)。256 个路由专家 + 1 个共享专家 + top-8 路由 + 2.5 缩放，是 u5 路由器一讲的全部输入；`GLM52_RMS_EPS` 是「全模型每个 RMSNorm 共用的唯一 eps」，由 probe 校验后以 f32 固化。

**indexer 与 rope theta**——[src/config.rs:L52-L59](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L52-L59)。`INDEX_TOPK=2048` 是 DSA 稀疏注意力的核心数字：每个 query token 只对 2048 个 KV 槽位做完整注意力；`NEXTN_LAYERS=1` 对应 `num_nextn_predict_layers`（native MTP 只有一层）。`ROPE_THETA = 8_000_000.0` 藏在 rope_parameters 里，probe 用浮点容差核对（见 4.2）。

#### 4.1.4 代码实践（源码阅读 + 手推）

1. **实践目标**：不看源码答案，手推派生常量，再回源码核对。
2. **操作步骤**：
   - 只用基础量 `HEADS=64, NOPE=192, ROPE=64, V=256, KV_LORA=512`，笔算 `QK_HEAD_DIM / Q_B_OUT / KV_A_OUT / KV_B_OUT / O_PROJ_IN / ROPE_HALF / SM_SCALE` 七个值。
   - 打开 [src/config.rs:L20-L39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L20-L39) 逐个对照。
3. **需要观察的现象**：你的手推值与源码表达式完全一致；特别注意 `KV_B_OUT` 是 \(64 \times (192+256) = 28672\)，不是 \(64 \times 256\)。
4. **预期结果**：16384 / 576 / 28672 / 16384 / 32 / 0.0625。另一个佐证：[src/model/build.rs:L97-L102](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/build.rs#L97-L102) 里 `kv_a_proj_with_mqa` 的读入行数硬编码为 576，正是 `KV_A_OUT`——权重形状与几何常量处处互证。
5. 本实践为纯静态阅读，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：`GLM52_MAX_CONTEXT` 是多少的幂？它除了描述 checkpoint 还在本 crate 哪里被用作边界？
答案：\(2^{20} = 1\,048\,576\)。在 `derive_max_model_len`（lib.rs，u2-l4 详讲）里它是显式 `--max-model-len` 的上限校验值，也是自动推导二分搜索的上界。

**练习 2**：为什么 `SM_SCALE` 是 f32 而不是 f64？
答案：[src/config.rs:L48-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L48-L50) 注释说明：它是 GPU norm/attention kernel 消费的值，kernel 以 f32 计算，先在 host 侧转换避免每次使用重复转换（eps 同理有 `GLM52_RMS_NORM_EPS`(f64) 与 `GLM52_RMS_EPS`(f32) 两个形态）。

**练习 3**：`HEAD_DIM=192` 与 `QK_NOPE_HEAD_DIM=192` 相等是巧合吗？
答案：从 probe 看，[src/config.rs:L102-L110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L102-L110) 把两者作为独立字段分别核对（`head_dim` 与 `qk_nope_head_dim`），数值相等是 GLM5.2 这一代 checkpoint 的选择，代码并未假设二者恒等。

### 4.2 probe_config_json 校验链

#### 4.2.1 概念说明

`probe_config_json(&serde_json::Value) -> Result<()>` 是一份「白名单契约」：config.json 里与本引擎行为相关的每一个字段都被逐一核对，**既不忽略多余字段，也不接受任何偏差**。它的调用点有两处，形成双保险：

1. **识别期**：`ModelLine::probe`——pegainfer-server 拿到 config.json 先问各模型线「这是你的模型吗」。GLM5.2 的回答是「`model_type` 必须是 `glm_moe_dsa`，且全字段通过 probe」。
2. **启动期**：`validate_startup` 在 launch 链路里重新读盘、重新解析、重新 probe——即使识别期过了，真正启动时仍然以磁盘上的文件为准再核一遍。

哲学：**探测与加载分离，探测失败的成本几乎为零**（读一个 JSON + 几十个比较），而「带着错误几何跑起来」的代价是数 GiB 显存分配错误甚至死锁。

#### 4.2.2 核心流程

```text
路径 A（识别）: pegainfer-server 读 config.json
  → Glm52Line::probe (model_line.rs)
    → model_type == "glm_moe_dsa" ? 否 → 报错「不是 GLM5.2」
    → probe_config_json(json) 逐字段校验

路径 B（启动）: launch (lib.rs)
  → validate_startup (lib.rs)
    → std::fs::read_to_string(config.json) + serde_json 解析
    → probe_config_json(json) 再核一遍
    → 通过后继续 Glm52WeightManifest::from_model_dir …（u2-l2）
```

probe 内部按「身份门 → 全局几何 → MLA/rope → MoE 路由 → indexer/MTP/词表 → 三个复合校验器」的顺序执行，任一 `ensure!` 失败立即带上下文返回错误。

#### 4.2.3 源码精读

**身份门**——[src/config.rs:L70-L92](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L70-L92)：先 `bail!` 检查 `model_type == "glm_moe_dsa"`；再要求 `architectures` 数组包含 `GlmMoeDsaForCausalLM`；接着钉死三个「引擎假设型」字段——`dtype` 必须是 `bfloat16`、`attention_bias` 必须是 `false`（本引擎的投影不带 bias）、`hidden_act` 必须是 `silu`。这些不是几何数字，而是「实现只支持这一种」的声明。

**全局几何等值核对**——[src/config.rs:L94-L100](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L94-L100)：hidden/vocab/层数/前 3 层 dense/上下文上限/两个中间维，全部走 `ensure_eq_usize`，错误消息形如 `hidden_size mismatch: got 6143, expected 6144`。

**MLA 维度与 rope**——[src/config.rs:L102-L127](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L102-L127)：八个 MLA 字段逐一等值核对；`rope_interleave` 必须为 `true`（GLM 系的交织式 rope 排布，indexer 的 `indexer_rope_interleave` 同样为 true，见 [L164-L167](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L164-L167)）；嵌套的 `rope_parameters` 子对象要求 `rope_type == "default"`，`rope_theta` 用浮点容差核对。

**MoE 路由契约**——[src/config.rs:L129-L157](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L129-L157)：这条链把 u5 将讲的路由器行为在配置层钉死——`n_group=1`、`topk_group=1`（无分组路由）、`topk_method="noaux_tc"`、`scoring_func="sigmoid"`、`norm_topk_prob=true`、`routed_scaling_factor=2.5`、`rms_norm_eps=1e-5`。注意浮点字段用 `ensure_float_close` 而非精确相等，容差如下表：

| 字段 | 期望值 | 容差 |
|------|--------|------|
| `rope_theta` | 8 000 000.0 | 1e-6 |
| `routed_scaling_factor` | 2.5 | 1e-12 |
| `rms_norm_eps` | 1e-5 | 1e-12 |

容差实现见 [src/config.rs:L299-L305](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L299-L305)。

**indexer/MTP/词表**——[src/config.rs:L159-L177](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L159-L177)：indexer 五字段等值核对；`num_nextn_predict_layers=1`；`index_share_for_mtp_iteration` 必须为 `true`（MTP 复用 indexer 的 top-k 结果）；`tie_word_embeddings` 必须为 `false`（embedding 与 lm_head 不共享权重，权重清单按两个独立张量规划）。

**收口与两个调用点**——[src/config.rs:L178-L183](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L178-L183) 依次调用三个复合校验器（4.3 详讲）后返回 `Ok(())`。[src/model_line.rs:L94-L100](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L94-L100) 是识别期调用；[src/lib.rs:L1641-L1646](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1641-L1646) 是启动期调用（读盘 → 解析 → probe），通过后才进入权重清单构建。[src/lib.rs:L53-L55](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L53-L55) 把它再导出为 crate 级符号。

#### 4.2.4 代码实践

1. **实践目标**：拿一份真实 GLM5.2 checkpoint 的 config.json，对照 probe 清单逐项核对，并预测若干「篡改」会命中哪个校验分支。
2. **操作步骤**：
   - 若本地有模型目录（`models/GLM5.2/config.json`），执行：
     ```bash
     jq '{model_type, architectures, dtype, hidden_size, vocab_size, num_hidden_layers,
          first_k_dense_replace, max_position_embeddings,
          q_lora_rank, kv_lora_rank, qk_nope_head_dim, qk_rope_head_dim, qk_head_dim, v_head_dim,
          n_routed_experts, num_experts_per_tok, n_shared_experts, topk_method, routed_scaling_factor,
          index_topk, index_head_dim, index_n_heads, num_nextn_predict_layers}' models/GLM5.2/config.json
     ```
   - 把输出与本讲 4.1.2 的常量表逐项比对。
   - 静态推演五种篡改各自命中哪一行：`model_type` 改成 `"qwen3"`；`hidden_size` 改成 6143；`topk_method` 改成 `"softmax"`；删掉 `rope_parameters`；`quantization_config.fmt` 改成 `"e5m2"`。
3. **需要观察的现象**：jq 输出与常量表全部一致；五种篡改分别命中：model_line.rs 的前置 `model_type` 门（甚至轮不到 probe）、`ensure_eq_usize` 的 hidden_size、topk_method 字符串门、`rope_parameters` 缺失的 `anyhow!`、fp8 校验器的 `fmt` 门。
4. **预期结果**：若模型目录存在，jq 输出与源码常量零偏差。无本地模型时，本实践退化为「校验清单走查」，全部结论可由源码静态得出；实际运行 jq 属于**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rope_theta` 的容差是 1e-6 而 `routed_scaling_factor` 是 1e-12？
答案：theta 数值大（\(8\times10^6\)），JSON 浮点序列化/解析在大量级上的相对误差需要更宽的绝对容差；scaling factor 是 O(1) 量级的超参，要求近乎精确。两者都只是防御浮点表示误差，不容忍语义偏差。

**练习 2**：probe 对 config.json 里**多余**的字段（比如某个陌生的 `"foo": 1`）会怎样？
答案：直接忽略。probe 是「对关心的字段逐个核对」的白名单，不校验 schema 完整性；多余字段不影响引擎行为，也就不需要拒绝。

**练习 3**：`probe_config_json` 为什么接收 `&Value` 而不是 `&Path`？
答案：解耦 IO 与逻辑：识别期（model_line.rs）由 server 传入已解析的 JSON，启动期（lib.rs validate_startup）自己读盘解析后传入。纯函数形态让它可以在任何上下文复用，也符合本 crate「探测无副作用」的设计。

### 4.3 层类型与 fp8 配置校验

#### 4.3.1 概念说明

78 层不是铁板一块，有两个正交的「层间差异轴」：

1. **MLP 轴**：`mlp_layer_types[l]` 是 `dense` 还是 `sparse`——前 3 层 dense（12 288 中间维的普通 SwiGLU MLP），第 3..77 层 sparse（256+1 专家 MoE）。
2. **indexer 轴**：`indexer_types[l]` 是 `full` 还是 `shared`——只有 full 层拥有自己的 indexer 权重并计算 2048-topk；shared 层直接复用「最近一个 full 层」的 top-k 结果（跨层共享，u4-l4 详讲）。

另外整个 checkpoint 的权重量化方式由 `quantization_config` 钉死：fp8 e4m3、动态激活量化、128×128 权重块——这是 u3-l1 FP8 原语的配置前提。

这三个校验器共同回答的问题是：「这份 checkpoint 的**逐层结构**与引擎编译进代码里的结构逐位一致吗？」

#### 4.3.2 核心流程

full indexer 层的推导规则（transformers 的推导在本 crate 的复刻）：

\[ \text{full}(l) \iff \max(l - (\text{offset}-1),\, 0) \bmod \text{freq} = 0,\quad \text{freq}=4,\ \text{offset}=3 \]

即 \( (l \,\dot-\, 2) \bmod 4 = 0 \)（\(\dot-\) 为饱和减法）。逐层代入：

- \(l = 0,1,2\)：饱和减法得 0，\(0 \bmod 4 = 0\) → **full**（前三层全 full）
- \(l = 3,4,5\)：得 1,2,3 → shared
- \(l = 6\)：得 4 → **full**；此后每 4 层一个：6, 10, 14, …
- \(l = 74\)：得 72 → **full**（最后一个）；75, 76, 77 → shared

所以 full 集合是 \(\{0,1,2\} \cup \{6,10,\dots,74\}\)，共 \(3 + 18 = 21\) 层，与源码注释「21 of 78 layers」一致。

#### 4.3.3 源码精读

**层类型谓词**——[src/config.rs:L61-L68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L61-L68)：`saturating_sub(GLM52_INDEX_SKIP_TOPK_OFFSET - 1)` 即饱和减 2，再 `is_multiple_of(4)`。注释直接写出了结果集合。

**mlp_layer_types 校验**——[src/config.rs:L185-L204](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L185-L204)：数组长度必须恰为 78；且第 `idx` 项必须等于 `idx < 3 ? "dense" : "sparse"`——引擎不接受「dense 层位置不同」的变体 checkpoint。

**indexer_types 校验**——[src/config.rs:L206-L218](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L206-L218)：只检查长度为 78 且每项 ∈ {full, shared}。注意这里**不**逐项核对 full/shared 的位置——因为位置不来自 config，而是来自上面的确定性谓词；config 只声明「这一层有没有自己的 indexer」。两者交叉验证：若 config 说第 4 层是 full 而谓词说 shared，权重加载时该层的 indexer 张量会找不到消费者（权重清单按谓词规划，u2-l2）。

**fp8 量化校验**——[src/config.rs:L220-L245](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L220-L245)：`quant_method == "fp8"`、`fmt == "e4m3"`、`activation_scheme == "dynamic"`、`weight_block_size == [128, 128]`。这四个值是 u3-l1 块量化原语（e4m3 + 128 块 scale）与 u5 DeepGEMM 路径的配置前提——任何一个不同，反量化数学就错了。

**谓词的真实消费点一：KV 页布局**——[src/model/mod.rs:L180-L192](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L180-L192)：`glm52_page_layout` 为每层先切一个 MLA 槽位，再**当且仅当** `glm52_layer_has_full_indexer(layer)` 时追加一个 index-K 槽位。78 层里 21 个 full 层 → 每页有 78 个 MLA 切片 + 21 个 index-K 切片（这正是 u4-l2 页几何的来源）。函数末尾 `assert_eq!(offset, GLM52_KV_PAGE_CONTENT_BYTES)` 保证布局与字节常量互证。

**谓词的真实消费点二：权重装载**——[src/model/build.rs:L111-L124](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/build.rs#L111-L124)：full 层才从 manifest 取 `self_attn.indexer.*` 权重（k_norm 的 weight/bias、weights_proj）构造 `Glm52LayerIndexer::Full`；shared 层构造为 `Shared`，根本不持有 indexer 权重指针——这就是 u4-l4 实践任务里「shared 层不会引用空指针」的机制保证。

#### 4.3.4 代码实践

1. **实践目标**：不看答案，用谓词 \( (l \,\dot-\, 2) \bmod 4 = 0 \) 手算 0..77 的 full/shared 序列，再与源码注释核对。
2. **操作步骤**：
   - 在纸上列出 l = 0..12 的判定（这是最容易出错的饱和减法区间）。
   - 写出 full 层完整集合并数个数。
   - 回答两个问题：第 3 层（第一个 sparse MLP 层）的 indexer 是什么类型？第 6 层为何特殊？
   - 进阶：用一条 shell 命令验证（awk 模拟饱和减法）：
     ```bash
     seq 0 77 | awk '{d=($1<2)?0:($1-2); if(d%4==0) printf "%d ", $1} END {print ""}'
     ```
3. **需要观察的现象**：awk 输出 `0 1 2 6 10 14 ... 74`，共 21 个数。
4. **预期结果**：第 3 层是 shared（它的 top-k 复用第 2 层的）；第 6 层是第一个「sparse MLP + full indexer」层——u6-l1 的实践正是拿第 6 层画完整数据流。awk 命令可在任何机器运行；若不方便执行，手工代入公式同样可完成验证。
5. 命令输出**待本地验证**（公式推导本身不依赖运行）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 full indexer 谓词不直接读 `indexer_types` 数组，而要在代码里重算？
答案：谓词是「引擎规划内存与权重时使用的唯一真值」（页布局、权重装载都调用它）；config 数组只是 checkpoint 侧的声明。重算保证布局/装载与 config 声明使用同一套规则，且省去在 GPU 热路径上查表——谓词是一次减法加一次取模。

**练习 2**：`n_group=1`、`topk_group=1` 意味着什么路由形态？
答案：DeepSeek 系常用的「分组路由 + 组内 topk」被退化为单组：sigmoid 打分后直接全局 top-8（`topk_method=noaux_tc` 带偏置修正），没有组约束。这正是 u5-l1 路由器实现的简化前提。

**练习 3**：如果一份 checkpoint 的 `weight_block_size` 是 `[64, 128]`，会在哪一步失败？
答案：启动期 `validate_startup` → `probe_config_json` → `ensure_fp8_quantization`（[src/config.rs:L240-L243](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L240-L243)）报 `weight_block_size must be [128, 128]`。引擎的块反量化 kernel 硬编码 128×128，接受 64×128 会在 u3-l1 的 scale 索引数学上全错。

## 5. 综合实践

**任务：制作一份「GLM5.2 几何速查卡」并用篡改推演验证 probe 链。**

1. **速查卡**：把 4.1.2 的常量分组整理成一页 Markdown 表格，给每个派生常量标注推导公式和一行「消费它的模块」（如 `KV_A_OUT=576` → `model/build.rs` 的 kv_a 读入行数、`mla_front.rs` 的投影宽度）。
2. **篡改推演**：复制一份真实 config.json 到临时目录，依次做六处篡改（model_type、hidden_size、mlp_layer_types[5] 改 "dense"、indexer_types[6] 改 "shared"、weight_block_size、删除 rope_parameters），每处写下你预测的错误消息原文，再到 [src/config.rs:L70-L245](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L70-L245) 找到会产出该消息的确切行号。注意区分：哪几处篡改会在 `ModelLine::probe` 就被拦下，哪几处要等到 `validate_startup`（两处都会被拦——但想清楚为什么不能只靠其中一处）。
3. **有 GPU 与权重时**（可选，**待本地验证**）：用篡改后的目录跑 `cargo run --release --features glm52 -- --model-path <篡改目录>`，确认启动在权重加载之前失败、错误消息与你的预测一致。
4. 产出：速查卡 + 六行「篡改 → 预测消息 → 源码行号」对照表。这张卡是后续 u3（算子形状）、u4（页几何）、u5（专家分片 256/EP 宽度）反复要回来查的底表。

## 6. 本讲小结

- GLM5.2 的全部几何以 `const` 钉死在 [src/config.rs:L9-L59](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L9-L59)，派生常量用表达式书写，形状成为编译期事实，为 CUDA Graph 与类型化缓冲铺路。
- `probe_config_json` 是白名单式 fail-fast 契约：身份门 → 全局几何 → MLA/rope → MoE 路由 → indexer/MTP → 三复合校验器，在 `ModelLine::probe`（识别期）与 `validate_startup`（启动期）各调用一次。
- full indexer 层由确定性谓词 \((l \,\dot-\, 2) \bmod 4 = 0\) 给出：\(\{0,1,2\}\cup\{6,10,\dots,74\}\) 共 21 层；KV 页布局（78 MLA + 21 index-K 切片）与权重装载都以它为准。
- fp8 契约（e4m3 / dynamic / 128×128 块）同样在 probe 层钉死，是 u3-l1 块量化原语的配置前提。
- 浮点超参（theta/scaling/eps）用分级容差核对——容忍浮点表示误差，不容忍语义偏差。

## 7. 下一步学习建议

下一讲 **u2-l2（checkpoint 布局与权重清单）**顺流而下：probe 通过后，`Glm52WeightManifest::from_model_dir` 解析 `model.safetensors.index.json`，把这些几何常量变成具体的张量名 → rank 加载包映射（非专家栈全量复制、256 专家按 rank 区间切分、第 78 层 MTP 权重归属）。阅读顺序建议：先看 `src/weights.rs` 的 manifest 解析，再回来看本讲的 `GLM52_MTP_LAYER` 如何决定 MTP 权重的装载条件。若想提前感受几何的消费现场，可跳读 `src/model/mod.rs` 的 `glm52_page_layout`（本讲 4.3.3 已引用）——它是 u4-l2 的完整预告。
