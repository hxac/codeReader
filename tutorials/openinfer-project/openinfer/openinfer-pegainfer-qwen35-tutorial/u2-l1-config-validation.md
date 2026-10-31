# u2-l1 config.json 的解析与校验：从 RawConfig 到 Config35

## 1. 本讲目标

学完本讲，你应该能够：

1. 追踪一条完整的加载链：磁盘上的 `config.json` → serde 反序列化成 `RawConfig` → `TryFrom` 一次性校验成 `Config35`。
2. 说出 `layer_types` 字符串数组如何决定每一层是 `FullAttention` 还是 `LinearAttention`，以及这条规则的「混合架构」背景。
3. 解释 `selection_vocab` 与 `decodable_vocab` 两个词表宽度的计算公式、128 对齐的动机，以及「tokenizer 比检查点宽就拒绝加载」的 fail-closed 规则。
4. 逐条列出 `TryFrom` 实现拒绝的非法组合，并对应到 `ConfigError` 的具体变体。

## 2. 前置知识

- **serde 反序列化**：Rust 最常用的序列化框架。给结构体标上 `#[derive(Deserialize)]`，`serde_json::from_str` 就能把 JSON 文本变成结构体实例。字段类型不匹配、缺了非 `Option` 字段时 serde 直接报错——这是第一道「形状」检查。
- **`TryFrom` trait**：Rust 标准库里「可能失败的类型转换」。`impl TryFrom<RawConfig> for Config35` 的意思是：从原始配置转成已验证配置这一步可能失败，失败时返回类型化的 `ConfigError`。它把「校验」收敛到一个编译器可见的边界上。
- **fail-closed（失败即关闭）**：宁可拒绝启动，也不带着可疑配置继续跑。与 fail-open（记个警告继续服务）相对。推理引擎里一个错误的维度会导致 CUDA 内核越界或垃圾输出，所以配置加载选择 fail-closed。
- **「不可表示非法状态」**：本 crate 反复出现的校验风格——校验只在构造边界做一次，之后下游拿到的类型（这里是 `Config35`）不可能处于非法状态，下游代码因此不需要重复防御。
- **词表与 padding 的背景**：检查点的 `vocab_size`（词嵌入矩阵的行数）常常比 tokenizer 实际能解码的 token 数多。多出来的行是训练时对齐硬件的 padding，推理时永远不该被选中。
- **GQA / 线性注意力头**：全注意力层里 `num_attention_heads` 个 Q 头共享 `num_key_value_heads` 个 KV 头（GQA，要求前者是后者的整数倍）；线性注意力层有自己的 `linear_num_key_heads` / `linear_num_value_heads`，同样要求整除。这些是第 3 单元的主角，本讲只需要知道「整除性是配置期就要保证的不变量」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/config/mod.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/mod.rs) | config 子模块的目录页：声明 `error` / `model` / `tp` 三个子模块与方向性边界；提供身份探测 `probe_config_json` |
| [src/config/model.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs) | 本讲主角：`RawConfig` / `TextConfig` 反序列化、`Config35` 定义、`TryFrom` 校验实现、`bound_selection_vocab` 词表宽度计算，以及内嵌单元测试 |
| [src/config/error.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/error.rs) | 类型化错误枚举 `ConfigError`：模型配置、TP 分配两大类共 14 个变体 |
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs) | 调用方：`from_safetensors*` 里以 `Config35::from_file` 起步、随后调用 `bound_selection_vocab`（下一讲的主角，本讲只看入口几行） |
| pegainfer-core/src/decodable_vocab.rs | 跨模型共享：`decodable_vocab_width` 从 tokenizer 文件算出可解码宽度，`aligned_logits_rows` 做 128 对齐 |
| pegainfer-core/src/eos.rs | 跨模型共享：`eos_token_ids` 按 generation_config → config → text_config 优先级解析 EOS 集合 |

注意 `config/tp.rs` 属于下一讲（u2-l2），本讲刻意不展开——`Config35` 本身对张量并行一无所知，这是模块注释里明写的方向性边界。

## 4. 核心概念与源码讲解

### 4.1 serde 反序列化：磁盘上的 config.json 如何变成 RawConfig

#### 4.1.1 概念说明

Qwen3.5 的 `config.json` 是嵌套结构：顶层放全局字段和一个 `text_config` 块，块内才是文本模型的几何参数。本 crate 的策略是**忠实镜像磁盘结构**：定义与 JSON 一一对应的 `RawConfig` / `TextConfig` / `RopeParameters` 三个「原始」结构体，serde 只负责把 JSON 变成它们，**不做任何跨字段判断**。

这样切分带来一个清晰的两级错误通道：

- serde 负责**形状**：字段类型对不对、必填字段在不在。出错时走 `anyhow`（`from_file` 里 `serde_json::from_str(&content)?` 的 `?`）。
- `ConfigError` 负责**语义**：跨字段规则、内核兼容规则。这是 4.2 的内容。

一个容易忽略的细节：`tie_word_embeddings` 和 `max_position_embeddings` 在 `RawConfig` 的顶层和 `TextConfig` 里都是 `Option`——因为 HF 检查点这两个字段的位置不统一，可能在外层也可能在内层，「两处都没有」才是错误，而那要等到校验阶段用类型化变体报告，所以 serde 不能把它们定成必填。

#### 4.1.2 核心流程

```
config.json（磁盘 JSON 文本）
   │  fs::read_to_string
   ▼
serde_json::from_str → RawConfig { text_config, max_position_embeddings?, tie_word_embeddings? }
   │                     TextConfig { hidden_size, ..., layer_types: Vec<String>, ... }
   │  TryFrom<RawConfig> for Config35（4.2）
   ▼
Config35（已验证：layer_types 已变成 Vec<LayerType>，全部不变量成立）
```

身份探测发生在更早的地方：`probe_config_json` 只回答「这是不是 Qwen3.5」——`model_type == "qwen3_5"`、architectures 含 `Qwen3_5ForConditionalGeneration`、`text_config.model_type == "qwen3_5_text"` 三条全过才算数。模块注释明确说「尺寸与形状校验属于 config 加载器」，即探测与校验是两个职责。这正是 u1-l4 讲过的 probe 两段式，此处不重复。

#### 4.1.3 源码精读

原始结构体完整定义（注意与 JSON 字段名一一对应）：

> [src/config/model.rs:29-58](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L29-L58)：`TextConfig` / `RawConfig` 忠实镜像磁盘 JSON：`layer_types` 此时只是 `Vec<String>`，两个可挪位的字段是 `Option`，serde 阶段不做任何判断。

RoPE 只读两个键：

> [src/config/model.rs:22-27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L22-L27)：`RopeParameters` 只取 `rope_theta` 和 `partial_rotary_factor`——Qwen3.5 的 RoPE 参数收在 `text_config.rope_parameters` 子对象里，且是部分旋转（partial RoPE）。

文件入口：

> [src/config/mod.rs:24-51](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/mod.rs#L24-L51)：`probe_config_json` 三条身份检查，全过返回 `Ok(())`，任一不过即 `bail!`/`ensure!` 拒绝。

> [src/config/model.rs:119-128](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L119-L128)：`from_file` 是完整加载入口——读文件、serde 反序列化、`try_from` 校验、最后解析 EOS。四步各自可能失败，全部用 `?` 上抛。

一个值得注意的旁证：测试夹具 JSON 里的 `text_config` 带了一个 `"eos_token_id": 0` 键，但 `TextConfig` 根本没有这个字段——serde 默认忽略未知键，EOS 实际由 `pegainfer_core::eos::eos_token_ids` 在 `from_file` 里另行解析（见 4.3.3）。这印证了「原始结构体只镜像自己关心的键」。

#### 4.1.4 代码实践

1. **实践目标**：亲手把一份真实（或夹具）config.json 映射到 `RawConfig` 的字段上。
2. **操作步骤**：
   - 若本地有 Qwen3.5 权重目录，打开 `models/Qwen3.5-4B/config.json`；没有就用 [src/config/model.rs:303-324](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L303-L324) 的 `VALID_RAW` 夹具。
   - 逐个字段抄写到一张两列表：左列 JSON 路径（如 `text_config.linear_key_head_dim`），右列 Rust 结构体字段（如 `TextConfig::linear_key_head_dim`）。
   - 特别标记三个问题字段：`tie_word_embeddings` 和 `max_position_embeddings` 分别出现在 JSON 的哪一层？`layer_types` 是字符串数组还是对象？
3. **需要观察的现象**：HF 发布的检查点里这两个 `Option` 字段的位置确实不统一（有的在外层、有的在 `text_config` 里）；`layer_types` 是纯字符串数组（如 `["linear_attention", "full_attention", ...]`）。
4. **预期结果**：得到一张 20 行左右的映射表，其中没有任何一行需要「计算」——这就是 serde 层的全部职责。待本地验证（取决于你手上有没有真实检查点；夹具路径无需求验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TextConfig.hidden_size` 的类型从 `usize` 改成 `bool`，错误是在 serde 阶段还是 `TryFrom` 阶段报出来？

**答案**：serde 阶段。`serde_json::from_str` 发现 JSON 里是数字、目标类型是 `bool`，直接反序列化失败，走 `anyhow` 通道，根本到不了 `TryFrom`。

**练习 2**：为什么 `tie_word_embeddings` 在两个结构体里都是 `Option<bool>` 而不是 `bool` + `#[serde(default)]`？

**答案**：`#[serde(default)]` 会把「缺失」和「false」混为一谈。而这里的语义是「外层和内层至少出现一处才算有效」——区分「缺失」与「取值」正是后续 `t.tie_word_embeddings.or(root_tie_word_embeddings)` 回退逻辑的前提，缺失必须能被识别出来并转成 `MissingTieWordEmbeddings` 类型化错误。

### 4.2 校验边界模式：TryFrom 的一次性全量校验

#### 4.2.1 概念说明

`Config35` 是下游一切代码（权重加载、内核启动、调度器）共同消费的「已验证配置」。它的模块文档写着：每条跨字段与内核 AOT 规则都在 `try_from` / `from_file` 这**单一边界**上强制执行，下游只能观察到「已知可加载」的模型。这就是「不可表示非法状态」：不是在几十处调用点各自 `assert`，而是让非法配置根本变不成 `Config35`。

校验里最有意思的一类是**内核兼容门**。u1-l2 讲过：GDN 的 Triton AOT 内核在构建期就把 head dim 烘焙（bake）进了签名——`GDN_AOT_KEY_HEAD_DIM = 128`、`GDN_AOT_VALUE_HEAD_DIM = 128`；全注意力内核则固定 `head_dim = 256`；因果卷积解码内核支持 `linear_conv_kernel_dim ∈ 1..=4`。这些常量由 `pegainfer-kernels` 拥有，本模块只重导出。文档注释说得直白：「在这里通过的配置，就是内核能跑的配置」——如果不在加载期对拍，一个 `head_dim = 128` 的检查点会在第一次内核启动时才炸出难以理解的 CUDA 错误。

错误全部收进 `ConfigError` 枚举而不是自由字符串，价值有二：调用方和测试能对**确切变体**分支（`assert_eq!(err, ConfigError::MissingTieWordEmbeddings)` 这样写测试），加载器能带着精确信息 fail-closed。

#### 4.2.2 核心流程

`try_from` 的校验按固定顺序执行，**首错即返**：

```
1. tie_word_embeddings = text_config 值 .or(根值)     缺失 → MissingTieWordEmbeddings
2. layer_types: Vec<String> → Vec<LayerType>          未知字符串 → UnknownLayerType
3. layer_types.len() == num_hidden_layers             不等 → LayerTypeCountMismatch
4. rotary_dim = ⌊head_dim × partial_rotary_factor⌋    为 0 → ZeroRotaryDim
5. max_position_embeddings = text_config 值 .or(根值) 缺失 → MissingMaxPositionEmbeddings
                                                       为 0 → NonPositiveMaxPositionEmbeddings
6. linear_key/value_head_dim == 128/128（AOT 烘焙值）  不等 → GdnAotHeadDimMismatch
7. head_dim == 256                                    不等 → FullAttnHeadDimMismatch
8. 1 ≤ linear_conv_kernel_dim ≤ 4                     越界 → LinearConvKernelDim
9. linear_num_value_heads % linear_num_key_heads == 0（且后者非 0）
                                                      不整除 → LinearHeadDivisibility
10. num_attention_heads % num_key_value_heads == 0（且后者非 0）
                                                      不整除 → AttentionHeadDivisibility
全部通过 → 构造 Config35（字段原样搬运，selection/decodable_vocab 暂置 vocab_size）
```

其中第 4 步的旋转维度是**截断取整**：

\[
\text{rotary\_dim} = \lfloor \text{head\_dim} \times \text{partial\_rotary\_factor} \rfloor
\]

Rust 的 `as usize` 对浮点做向零截断。夹具里 \(256 \times 0.25 = 64\)，正好整除所以看不出差别；若因子取 0.3，则 \(256 \times 0.3 = 76.8 \to 76\)。部分旋转意味着 Q/K 向量只有前 `rotary_dim` 维参与 RoPE，其余维不旋转——这是 u3-l1 的主题，这里只需要保证算出的值非零。

#### 4.2.3 源码精读

类型与常量：

> [src/config/model.rs:15-20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L15-L20)：`LayerType` 只有两个变体——「模型级（与 TP 无关）的层排布」这一注释点明它属于 `Config35` 而非分片几何。

> [src/config/model.rs:60-68](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L60-L68)：三个内核常量从 `pegainfer_kernels::ops` 重导出（128/128/4），`FULL_ATTN_HEAD_DIM = 256` 是本模块自己的常量。注释说明 Qwen3.8-Flash-Next 线未来用同一组数字。

校验主体，按上面流程图的顺序逐段对应：

> [src/config/model.rs:197-217](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L197-L217)：字段回退（`or`）与 `LayerType` 字符串匹配（`"full_attention"` / `"linear_attention"` 之外即 `UnknownLayerType`）、长度对账。

> [src/config/model.rs:219-230](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L219-L230)：`rotary_dim` 截断计算与两个「存在性/正性」检查。

> [src/config/model.rs:232-253](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L232-L253)：三道内核门——GDN AOT head dim 对拍、全注意力 head dim 256、卷积核宽度 1..=4。

> [src/config/model.rs:254-271](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L254-L271)：两条整除性规则（线性头之间、GQA 的 Q/KV 头之间），都附带「分母非零」前置。

> [src/config/model.rs:273-295](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L273-L295)：通过后的构造——纯字段搬运，没有第二次计算；`selection_vocab` / `decodable_vocab` 先置为 `vocab_size`，等 4.3 的 `bound_selection_vocab` 收紧。

错误面：

> [src/config/error.rs:14-77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/error.rs#L14-L77)：`ConfigError` 全部 14 个变体。文件头注释强调：这里的每个失败都是**静态**不变量违反，发生在任何权重映射、任何 CUDA 显存分配之前。前 11 个属于模型配置，后 3 个（`TpZeroWorldSize` 等）属于下一讲的 TP 分配。

内嵌测试是活文档，每个变体几乎都有对应用例：

> [src/config/model.rs:373-387](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L373-L387)：用字符串替换制造非法 JSON（删掉 `tie_word_embeddings`、把 `linear_attention` 换成 `window_attention`），断言得到的**确切**错误变体——这正是类型化错误的设计回报。

#### 4.2.4 代码实践

1. **实践目标**：把 `TryFrom` 拒绝的非法组合整理成一张「构造方法 → 错误变体」对照表。
2. **操作步骤**：
   - 以 `VALID_RAW` 夹具为基底，按下表逐条制造非法变体（全部是字符串替换，仿照现有测试的写法）；
   - 对每条先**纸面预测**会命中哪个 `ConfigError` 变体（注意首错即返的顺序），再对照源码核对；
   - 有工具链时用 `cargo test --release -p pegainfer-qwen35 --features qwen35 --lib config::model` 跑现有内嵌测试确认环境（该命令需要 u1-l2 讲过的 CUDA/Triton 构建链，无 GPU 环境可跳过运行，纯纸面完成）。
3. **需要观察的现象**：同一份 JSON 同时违反两条规则时，只有**排在前面**的那条被报告（例如同时改坏 `layer_types` 计数和 `head_dim`，报 `LayerTypeCountMismatch` 而不是 `FullAttnHeadDimMismatch`）。
4. **预期结果**：一张如下形态的表（「如何构造」列均为夹具字符串替换）：

| 非法组合 | 如何构造（基于 VALID_RAW） | 错误变体 |
|---|---|---|
| 两层都没有 `tie_word_embeddings` | 删除顶层 `"tie_word_embeddings": true,` | `MissingTieWordEmbeddings` |
| 未知层类型 | `linear_attention` → `window_attention` | `UnknownLayerType("window_attention")` |
| `layer_types` 空但层数非 0 | 层数组改成 `["linear_attention"]`（层数 2） | `LayerTypeCountMismatch { actual: 1, expected: 2 }` |
| `partial_rotary_factor` 算出 0 | 改为 `0.0` | `ZeroRotaryDim` |
| 两层都没有 `max_position_embeddings` | 删除该行 | `MissingMaxPositionEmbeddings` |
| GDN head dim 与 AOT 不符 | `linear_key_head_dim` 4 改成 `64` | `GdnAotHeadDimMismatch` |
| 全注意力 head_dim ≠ 256 | `head_dim` 改成 `128` | `FullAttnHeadDimMismatch` |
| 卷积核宽超上限 | `linear_conv_kernel_dim` 4 改成 `5` | `LinearConvKernelDim { max: 4, actual: 5 }` |
| 线性头不整除 | `linear_num_value_heads` 48 改成 `17` | `LinearHeadDivisibility` |
| GQA 不整除 | `num_attention_heads` 4 保持、`num_key_value_heads` 2 改成 `3` | `AttentionHeadDivisibility` |

   注意一个诚实的边界：**空 `layer_types` 配上同为 0 的 `num_hidden_layers` 能通过 `TryFrom`**——第 3 步只对账长度，不要求非空；「0 层模型」会在更下游（权重加载找不到任何层）才暴露。校验边界保证的是它声明的那十条规则，不是宇宙中一切合理性。

#### 4.2.5 小练习与答案

**练习 1**：夹具的 `linear_num_key_heads = 16`、`linear_num_value_heads = 48`。为什么这个组合必须被接受？（提示：这不是随意数字。）

**答案**：48 = 16 × 3，满足 `LinearHeadDivisibility`；且真实 Qwen3.5 检查点就是 16 QK / 48 V 头。crate 里有专门的回归测试 `acceptance_of_48_value_heads`（[src/config/model.rs:428-432](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L428-L432)）守住这个组合能加载。

**练习 2**：为什么内核维度门放在配置校验里，而不是在内核启动时断言？

**答案**：AOT 内核的维度烘焙在构建期签名里，运行期改不了。若等到第一次内核启动才失败，用户看到的是 CUDA 层的晦涩报错，且此时显存可能已按错误维度分配。在加载边界对拍常量，代价是一次整数比较，收益是「能构造出 `Config35` ⇒ 内核一定接得住」这条全局保证。

**练习 3**：`Config35` 为什么不把 TP 的 rank/world_size 也校验进去？

**答案**：模块注释写明：张量并行分片是 `tp.rs` 的 `LocalGeometry` / `TensorParallelConfig` 的职责，`Config35` 描述的是「模型级、TP 无关」的几何。拆开的好处是单 GPU 路径完全不触碰 TP 类型，TP 校验错误（`TpIndivisible` 等）也有独立的错误变体。这是下一讲 u2-l2 的入口。

### 4.3 词表宽度计算：selection_vocab 与 decodable_vocab

#### 4.3.1 概念说明

`Config35` 里有两个看起来都像「词表大小」的字段，加上检查点自带的 `vocab_size`，一共三个宽度，各司其职：

| 字段 | 含义 | 谁用它 |
|---|---|---|
| `vocab_size` | 检查点词嵌入矩阵的物理行数 | 权重加载、嵌入查表 |
| `decodable_vocab` | tokenizer 真正能解码的 token 数（稠密前缀） | argmax 与采样路由的测量基准；超出部分被抑制 |
| `selection_vocab` | `decodable_vocab` 向上取整到 128 的倍数、再夹到 `vocab_size` 以内 | logits GEMM 的 M 维、logits 缓冲、采样器 arena |

为什么要对齐到 128？`pegainfer-core` 的文档注释给出动机：Qwen3.5-4B 实际可解码 248077 个 id，这是个「坏」宽度——未对齐的 GEMM 维度会把 cublasLt 逼到 align-1 内核，性能大幅受损；取整到 128-token tile 后走快路径。多出来的 `decodable_vocab..selection_vocab` 行是纯 padding，采样前会被压成 `-inf`，永远不可能被选中（pad 抑制的完整链路在 u5-l4 采样一讲）。

fail-closed 规则则是反向的边界：如果 tokenizer 比检查点还宽（`effective_vocab > vocab_size`），说明这个检查点根本服务不了这个 tokenizer，直接拒绝加载（`EffectiveVocabExceedsCheckpoint`），而不是悄悄截断输出空间。

#### 4.3.2 核心流程

三个宽度的推导关系（\(d\) 为可解码宽度，\(v\) 为检查点行数）：

\[
\text{selection\_vocab} = \min\left(\left\lceil \frac{d}{128} \right\rceil \times 128,\; v\right), \qquad d \le v \text{（否则拒绝）}
\]

完整时序（都发生在权重加载之前）：

```
from_file:
  RawConfig → Config35          （此时 selection/decodable_vocab 暂置为 vocab_size）
  eos_token_ids ← generation_config.json / config.json / text_config 三处按序取 eos_token_id
bound_selection_vocab(effective_vocab):     ← 由 weights.rs 在加载权重前调用
  effective_vocab = decodable_vocab_width(model_path)   （读 tokenizer 两个文件合并 id 集）
  1. effective_vocab > vocab_size ? → EffectiveVocabExceedsCheckpoint，拒绝
  2. decodable_vocab = effective_vocab
  3. selection_vocab = aligned_logits_rows(effective_vocab, vocab_size)
  4. selection_vocab != effective_vocab 时打印一条 info 日志（tile 对齐 padding）
```

用真实数字手算一遍 Qwen3.5-4B（\(d = 248077\)，\(v = 248320\)，均为仓库文档记载的真实值）：

\[
\left\lceil \frac{248077}{128} \right\rceil \times 128 = 1939 \times 128 = 248192, \qquad \min(248192,\; 248320) = 248192
\]

所以 `selection_vocab = 248192`、`decodable_vocab = 248077`，padding 区间是行 248077..248192（115 行）。仓库的性能归因文档里 lm_head 那一行写的正是 248192——理论手算与线上实测对上了。

#### 4.3.3 源码精读

> [src/config/model.rs:107-116](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L107-L116)：两个字段的文档注释——`selection_vocab` 是「token 选择宽度」，`decodable_vocab` 之外的行被压 `-inf`，且 argmax 与采样路由的测量基准是它。

> [src/config/model.rs:159-180](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L159-L180)：`bound_selection_vocab` 全文——先拒绝过宽 tokenizer（fail-closed），再记 `decodable_vocab`，最后调 `aligned_logits_rows` 算对齐宽度。注释点明这条规则集中在校验边界而不是散落在加载器各处。

> [pegainfer-core/src/decodable_vocab.rs:112-114](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/decodable_vocab.rs#L112-L114)：对齐公式本体，一行：`decodable.next_multiple_of(128).min(vocab)`。

> [pegainfer-core/src/decodable_vocab.rs:69-104](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/decodable_vocab.rs#L69-L104)：`decodable_vocab_width` 镜像前端（vLLM）的分词器合并规则：`tokenizer.json` 的 vocab + added_tokens（解析失败即致命），并上 `tokenizer_config.json` 的 added_tokens_decoder（解析失败则带警告整组跳过）。末尾要求 id 集合构成**稠密前缀**（`max_id + 1 == width`）——行区间选择界掩不住空洞，稀疏 id 空间同样 fail-closed。

> [src/weights.rs:161-184](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L161-L184)：调用点——`from_file` 之后、mmap 权重分片之前，`decodable_vocab_width` → `bound_selection_vocab`，宽度有 padding 时打一条 info。顺序说明词表裁定必须发生在按 `selection_vocab` 分配 logits 缓冲之前。

> [pegainfer-core/src/eos.rs:18-59](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/eos.rs#L18-L59)：`from_file` 的收尾——按 generation_config → config → text_config 的顺序找第一个声明了 `eos_token_id` 的来源，接受单值或数组，逐个校验 `< vocab_size` 且去重。找不到任何声明则报错（没有 EOS 的模型无法正常停止生成）。

内嵌测试锁定对齐行为：

> [src/config/model.rs:342-370](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L342-L370)：`vocab_size = 1000` 下，977 → 取整 1024 被夹到 1000（`selection=1000, decodable=977`）；769 → 896（低于检查点行数，padding 区间 769..896）；1001 → 直接 `EffectiveVocabExceedsCheckpoint`。

#### 4.3.4 代码实践

1. **实践目标**：不写代码，纯手算 Qwen3.5-4B 的两个词表宽度，并用测试断言核对方法。
2. **操作步骤**：
   - 取真实参数：`vocab_size = 248320`（检查点），`effective_vocab = 248077`（tokenizer 可解码宽度，仓库文档记载值）；
   - 用公式 \(\min(\lceil d/128 \rceil \times 128, v)\) 手算 `selection_vocab`，写出 padding 区间；
   - 再算夹具规模的两道小题：\(v=1000, d=977\) 与 \(v=1000, d=769\)；
   - 把你的四个答案与 [src/config/model.rs:342-358](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L342-L358) 的断言逐一对照（后两题就是测试原文）。
3. **需要观察的现象**：977 的取整结果 1024 **超过**检查点行数 1000，被 `min` 夹回来——「取整」和「夹紧」两步缺一不可；769 的取整结果 896 未超，得以保留。
4. **预期结果**：
   - Qwen3.5-4B：`selection_vocab = 248192`，`decodable_vocab = 248077`，padding 行 115；
   - 夹具：`(1000, 977)` → `(1000, 977)`；`(1000, 769)` → `(896, 769)`。
   - 若手上有真实模型目录，可读取其 `tokenizer.json` 数 id、`config.json` 取 `text_config.vocab_size` 后重算一遍（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接把 `selection_vocab` 定成 `vocab_size`（反正 248320 也是 128 的倍数）？

**答案**：那样 logits GEMM 每步都要多算 \(248320 - 248192 = 128\) 行、采样器 arena 也按更宽的行分配。以 Qwen3.5-4B 为例差距只有 128 行（~0.05%），但当 tokenizer 明显窄于检查点时（夹具里 769 对 1000 差 17%），节省可观。宽度规则的本质是「算且只算可能被选中的行」。

**练习 2**：`decodable_vocab_width` 为什么要求 id 空间稠密（`max_id + 1 == width`）？

**答案**：`selection_vocab` 是一个**行区间**界——它只能表达「前 N 行参与选择」。如果 id 空间有洞（比如有 id 5 和 7 但没有 6），区间无法把 7 排除在 5 之内而保留 5；带着这种空洞继续跑会静默截断输出空间，所以直接拒绝加载。又是同一条 fail-closed 原则。

**练习 3**：`from_file` 里 EOS 解析为什么放在 `try_from` 之后、`bound_selection_vocab` 之前？

**答案**：EOS 校验需要 `vocab_size`（每个 id 必须 `< vocab_size`），所以必须在 `Config35` 构造之后；而它完全不看词表宽度，先后于 `bound_selection_vocab` 都不影响正确性——实际顺序只是「from_file 能做的都做完，把词表裁定留给有 tokenizer 信息的调用方（weights.rs）」这一职责切分的自然结果。

## 5. 综合实践

**任务：给一个检查点出具「配置体检报告」。**

以 `VALID_RAW` 夹具（或本地 `models/Qwen3.5-4B`）为对象，产出一份只依赖纸面推导的报告，把本讲三个模块串起来：

1. **反序列化面**（4.1）：列出 `RawConfig`/`TextConfig` 的全部字段及来源 JSON 路径；标出两个「可在两层出现」的 `Option` 字段及其回退结果。
2. **校验面**（4.2）：逐条过 `TryFrom` 的十步流程，记录每步的输入值与结论；手算 `rotary_dim`（夹具：\(256 \times 0.25 = 64\)）；再自选两条规则，写出「如何改夹具触发对应 `ConfigError` 变体」。
3. **词表面**（4.3）：手算 `selection_vocab` / `decodable_vocab` 与 padding 区间（夹具无 tokenizer，可自设 \(d\) 值演练三档：\(d < v\) 且对齐后不超、对齐后超被夹、\(d > v\) 被拒）。
4. **验证**（可选，需 u1-l2 的构建链）：`cargo test --release -p pegainfer-qwen35 --features qwen35 --lib config::model` 跑通内嵌测试，确认你改的变体与断言一致。

检验标准：报告里的每个数字都能在对应源码行找到出处；三条被拒绝的构造各自只触发**一个**错误变体，且是你预测的那个。

## 6. 本讲小结

- 配置加载是两级错误通道：serde 管「形状」（字段在不在、类型对不对），`ConfigError` 管「语义」（跨字段与内核不变量）；`Option` 字段的存在让「缺失」能被类型化报告而不是与取值混淆。
- `TryFrom<RawConfig> for Config35` 是唯一校验边界：十步有序检查、首错即返；通过之后下游拿到的配置「已知可加载」，无需重复防御。
- `layer_types` 在这里完成从字符串到 `LayerType` 的升格，混合架构（full/linear 两种层的排布）自此成为类型里的一等信息。
- 内核维度（128/128、256、1..=4）是构建期烘焙的 AOT 常量，配置期对拍让「内核不兼容」在加载时就 fail-closed，而不是首次启动时炸 CUDA 错误。
- 三个词表宽度各司其职：`vocab_size`（物理行）→ `decodable_vocab`（稠密可解码前缀，fail-closed 拒绝过宽 tokenizer 与稀疏 id）→ `selection_vocab`（128 对齐、夹紧，喂给 cublasLt 快路径）。
- `Config35` 刻意对张量并行无知——TP 几何留给下一讲的 `LocalGeometry`，那是方向性边界的另一半。

## 7. 下一步学习建议

下一讲 **u2-l2（张量并行几何：TensorParallelConfig 与 LocalGeometry）** 接着读 [src/config/tp.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs)：看同一个「不可表示非法状态」风格如何让非法 `(rank, world_size)` 根本构造不出来，以及本讲的头数如何被切成每 rank 的 `local_*` 字段（`TpIndivisible` 门）。

如果想先横向巩固本讲，建议：
- 重读 [src/config/error.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/error.rs) 的后三个 TP 变体，带着「它们会在哪被触发」的问题预告下一讲；
- 对照 pegainfer-core 的 [decodable_vocab.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/decodable_vocab.rs) 完整注释，理解前端（vLLM）分词器合并契约为什么要被引擎精确镜像；
- 之后再进入 u2-l3 权重加载，看 `Config35` + `LocalGeometry` 如何一起决定每个 safetensors 分片读到哪、切多少。
