# 模型几何：常量、配置探针与并行形状

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 Kimi-K2.6 的关键几何数字：hidden 7168、64 个注意力头、q_lora 1536、kv_lora 512、61 层（1 dense + 60 MoE）、384 个路由专家每 token 选 top-8、INT4 量化组大小 32。
2. 理解 `probe_config_json` 是一个「纯校验门」：它只判断一份 checkpoint 的 `config.json` 是否与 crate 编译期写死的常量完全一致，不读取权重、不分配任何资源；它同时接受 `kimi_k25`（外层包裹 `text_config`）与 `kimi_k2`（扁平）两种配置身份。
3. 会用 `KimiK2ParallelShape::tp8_ep8()` / `tp1_dp8()` 推导每个 rank 的本地维度：`heads_per_tp`、`local_experts`、`vocab_per_tp`，以及 `KimiLocalDims` 里的各 TP 本地张量宽度。

本讲只涉及一个源码文件（`src/config.rs`），不需要 GPU，不需要打开 `kimi-k2` feature——这是刻意设计的，后面会解释为什么。

## 2. 前置知识

- **hidden size（隐藏维）**：Transformer 每一层内部向量宽度。Kimi-K2.6 是 7168，属于「万亿参数级」模型的宽度量级。
- **MLA（Multi-head Latent Attention）**：DeepSeek 系列首创的注意力变体。普通 MHA 要给每个头缓存完整的 K/V；MLA 先把 K/V 压缩到一个低秩「潜在向量」（latent）再缓存，同时把 Q 也先压到低秩。本讲只需要记住三个数字的角色：
  - `q_lora_rank = 1536`：Q 的压缩秩（hidden 7168 先压到 1536，再升维成每头 192）；
  - `kv_lora_rank = 512`：KV 的压缩秩（每 token 每层只缓存 512 维潜在向量 + 64 维 RoPE 部分）；
  - 每头 Q 分成两段：128 维不做 RoPE（nope）+ 64 维做 RoPE（rope），所以 \( q_{\text{head\_dim}} = 128 + 64 = 192 \)。
- **MoE（Mixture of Experts）**：把 FFN 拆成 384 个「专家」，每个 token 由路由器只挑 8 个参与计算，另有 1 个所有 token 都经过的「共享专家」。
- **INT4 组量化**：权重用 4 bit 存储，每 32 个连续数值共享一个 scale（缩放因子），即 `int4 group size = 32`。
- **TP / DP / EP**：张量并行（一张矩阵按行/列切开分给多卡）、数据并行（不同请求分给不同卡）、专家并行（384 个专家切开分给多卡）。Kimi-K2.6 固定 8 卡，支持 TP8/DP1 与 TP1/DP8 两种拓扑。
- **`serde_json::Value`**：Rust 里表示任意 JSON 的动态树结构，`json.get(key)` 取字段。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
|---|---|---|
| `pegainfer-kimi-k2/src/config.rs` | 本讲主角：模型几何常量、配置探针 `probe_config_json`、并行形状 `KimiK2ParallelShape` / `KimiLocalDims` | 全文（仅 359 行） |
| `pegainfer-kimi-k2/src/lib.rs` | crate 根：模块声明、`start_engine` 入口 | `pub(crate) mod config` 与唯一的 `pub use config::KIMI_K2_LAYERS` |
| `pegainfer-kimi-k2/src/model_line.rs` | Kimi 的 `ModelLine` trait 实现（服务端调度入口） | `probe()` 如何调用 `probe_config_json` |
| `pegainfer-kimi-k2/src/runner/bringup.rs` | 引擎启动 | `KimiK2ParallelShape` 的消费点（仅引用，细节留到第 3 单元） |
| `pegainfer-kimi-k2/src/weights/manifest.rs` | 权重清单 | `rank_plan` 如何用 `heads_per_tp` / `local_experts` / `vocab_per_tp` 切区间（仅引用，细节留到第 2 单元） |

## 4. 核心概念与源码讲解

### 4.1 几何常量表

#### 4.1.1 概念说明

这个 crate 对 Kimi-K2.6 的全部几何假设都写死在一组 `const` 里，而不是从 checkpoint 的 `config.json` 动态读入再泛型适配。原因有三：

1. **类型化 scratch 的需要**。后续（第 5 单元）会看到解码缓冲用 `generic_const_exprs` 把维度放进类型系统，编译期就要知道每个维度。
2. **只服务一个模型线**。crate 名字就叫 `pegainfer-kimi-k2`，不支持「任意 hidden size」是合理的范围收敛。
3. **把「配置错了」提前到启动第一毫秒**。几何不匹配的 checkpoint 在探针阶段就被拒绝，而不是加载到一半才在某个 GEMM 里炸出形状错误。

所以这份常量表就是 crate 与 checkpoint 之间的**合同正文**，而 4.2 节的 `probe_config_json` 是验合同的海关。

#### 4.1.2 核心流程

常量本身没有执行流程，但可以按「一个 token 走过一层」的视角组织：

```text
输入 hidden(7168)
  ├─ 注意力（MLA）
  │    q_lora 1536 ← hidden 7168 下投影
  │    kv_lora 512 ← hidden 7168 下投影（缓存的就是它 + 64 维 rope）
  │    每头: q 192 = nope 128 + rope 64；v 输出 128；共 64 头
  ├─ 第 0 层 FFN：dense，intermediate 18432
  └─ 第 1..60 层 FFN：MoE，每专家 intermediate 2048 × top-8 激活 + 1 个共享专家
位置编码：YaRN，theta 5e4，原始窗口 4096 × factor 64
量化：INT4，组大小 32（compressed-tensors pack-quantized 格式）
```

一个值得记住的交叉验证：YaRN 把原始窗口外扩 64 倍，恰好等于声明的最大上下文

\[ 4096 \times 64 = 262\,144 = KIMI\_K2\_MAX\_CONTEXT \]

#### 4.1.3 源码精读

常量全部集中在文件头部：

- [pegainfer-kimi-k2/src/config.rs:13-18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L13-L18) 定义整体尺寸：hidden 7168、词表 163840、61 层 = 1 dense + 60 MoE、最大上下文 262144。注意 `KIMI_K2_LAYERS` 是唯一不带 `pub(crate)` 的（即完全 `pub`）——它是 crate 对外导出的唯一常量。
- [pegainfer-kimi-k2/src/config.rs:20-26](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L20-L26) 定义 MLA 头几何：64 头、q_lora 1536、kv_lora 512、nope 128、rope 64，且 `KIMI_K2_Q_HEAD_DIM` 不是独立常量而是编译期表达式 `KIMI_K2_QK_NOPE_HEAD_DIM + KIMI_K2_QK_ROPE_HEAD_DIM`（=192），v 每头输出 128。
- [pegainfer-kimi-k2/src/config.rs:28-33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L28-L33) 定义 FFN/MoE/量化：dense 层 intermediate 18432、每专家 intermediate 2048、384 路由专家、top-8、1 共享专家、INT4 组 32。
- [pegainfer-kimi-k2/src/config.rs:35-41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L35-L41) 定义 RoPE/YaRN 与归一化：theta 5 万、YaRN factor 64、原始窗口 4096、beta_fast 32 / beta_slow 1、路由缩放 2.827、RMSNorm eps 1e-5。

文件第 3–5 行有一处容易忽略但很关键的注释与属性：

- [pegainfer-kimi-k2/src/config.rs:3-5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L3-L5) 说明这些常量与形状助手「刻意保持在不开 feature 时也能编译」，并用 `cfg_attr(not(feature = "kimi-k2"), allow(dead_code))` 压掉 feature-off 构建下的死代码警告——因为不开 `kimi-k2` 时它们的主要消费者（`model_line`、`runner`、`weights`）都不存在。

对应 crate 根的模块声明与导出：

- [pegainfer-kimi-k2/src/lib.rs:24](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L24) `pub(crate) mod config` 不带 feature 门，任何构建都编译它。
- [pegainfer-kimi-k2/src/lib.rs:36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L36) `pub use config::KIMI_K2_LAYERS;` 是唯一的对外导出（供其他 crate 引用层数，比如 kernel report 统计）。

汇总成表（建议抄进笔记，后面所有讲义都会反复用到）：

| 常量 | 值 | 含义 |
|---|---|---|
| `KIMI_K2_HIDDEN` | 7168 | 隐藏维 |
| `KIMI_K2_VOCAB` | 163 840 | 词表大小 |
| `KIMI_K2_LAYERS` | 61 | 总层数（唯一 `pub`） |
| `KIMI_K2_DENSE_LAYERS` | 1 | 前 1 层是 dense FFN |
| `KIMI_K2_MOE_LAYERS` | 60 | 后 60 层是 MoE |
| `KIMI_K2_MAX_CONTEXT` | 262 144 | 最大上下文（= 4096 × 64） |
| `KIMI_K2_HEADS` | 64 | 注意力头数 |
| `KIMI_K2_Q_LORA_RANK` | 1536 | Q 压缩秩 |
| `KIMI_K2_KV_LORA_RANK` | 512 | KV 压缩秩（潜在缓存宽度） |
| `KIMI_K2_QK_NOPE_HEAD_DIM` | 128 | 每头不做 RoPE 的维数 |
| `KIMI_K2_QK_ROPE_HEAD_DIM` | 64 | 每头做 RoPE 的维数 |
| `KIMI_K2_Q_HEAD_DIM` | 192 | 128 + 64，派生表达式 |
| `KIMI_K2_V_HEAD_DIM` | 128 | 每头 V 输出维数 |
| `KIMI_K2_DENSE_INTERMEDIATE` | 18 432 | dense 层 FFN 中间维 |
| `KIMI_K2_EXPERT_INTERMEDIATE` | 2048 | 每个专家 FFN 中间维 |
| `KIMI_K2_ROUTED_EXPERTS` | 384 | 路由专家总数 |
| `KIMI_K2_TOPK` | 8 | 每 token 激活专家数 |
| `KIMI_K2_SHARED_EXPERTS` | 1 | 共享专家数 |
| `KIMI_K2_INT4_GROUP_SIZE` | 32 | INT4 组量化大小 |
| `KIMI_K2_ROPE_THETA` | 50 000.0 | RoPE 基频 |
| `KIMI_K2_YARN_FACTOR` | 64.0 | YaRN 外扩因子 |
| `KIMI_K2_YARN_ORIGINAL_MAX_POS` | 4096 | YaRN 原始窗口 |
| `KIMI_K2_YARN_BETA_FAST` / `BETA_SLOW` | 32.0 / 1.0 | YaRN 插值边界斜率 |
| `KIMI_K2_ROUTED_SCALING_FACTOR` | 2.827 | 路由输出缩放（见第 6 单元） |
| `KIMI_K2_RMS_NORM_EPS` | 1e-5 | RMSNorm epsilon |

#### 4.1.4 代码实践

**实践目标**：亲手确认「常量在 feature-off 构建下也可用」，并摸清每个常量的消费者。

**操作步骤**：

1. 在仓库根目录运行 `cargo check -p pegainfer-kimi-k2`（不带任何 feature）。预期编译通过，且 `config.rs` 参与编译。
2. 运行 `rg -n "KIMI_K2_" pegainfer-kimi-k2/src --stats`，统计每个常量被引用的文件分布。
3. 对照 [pegainfer-kimi-k2/src/lib.rs:36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L36)，用 `rg -n "KIMI_K2_LAYERS" --glob '!pegainfer-kimi-k2'` 看是否真的没有 crate 外消费者。

**需要观察的现象**：`cargo check` 无需 GPU、无需 `kimi-k2` feature 即可通过；`rg` 结果里 `runner/`、`weights/` 下的引用都集中在 feature 门之后的模块。

**预期结果**：这印证了 [pegainfer-kimi-k2/src/config.rs:3-5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L3-L5) 的注释——几何层永远可用。若 `cargo check` 在无 CUDA 环境报错，说明依赖了被 feature 触发的 kernels 构建，请回查命令是否漏了「不带 feature」。实际通过情况：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KIMI_K2_Q_HEAD_DIM` 写成 `KIMI_K2_QK_NOPE_HEAD_DIM + KIMI_K2_QK_ROPE_HEAD_DIM` 而不是直接写 192？

**答案**：MLA 里每头 Q 向量物理上就是两段拼接——128 维不参与 RoPE 旋转的子空间与 64 维参与的子空间。用表达式表达「192 是 128+64 的结果」这一结构事实，改任何一段时另一处自动同步，也向读者暴露了维度的来源。

**练习 2**：61 层里有多少层有路由专家？哪一层没有？

**答案**：60 层有（`KIMI_K2_MOE_LAYERS = 60`），只有第 0 层没有。第 0 层是 dense FFN（`KIMI_K2_DENSE_LAYERS = 1`，对应探针里的 `first_k_dense_replace = 1`）。

**练习 3**：每个 token 在一层 MoE 里经过的「激活 FFN 中间维」总量是多少（只算路由专家）？

**答案**：\( 2048 \times 8 = 16384 \)（每专家 2048 × top-8）。加上共享专家的 2048，一层共 18432——恰好等于 dense 层的 18432。这是 MoE 设计里常见的「激活量对齐 dense 基线」手法（数值巧合层面的一致，供记忆用）。

### 4.2 `probe_config_json` 校验门

#### 4.2.1 概念说明

`probe_config_json` 回答一个问题：**这份 `config.json` 是不是 crate 常量所描述的那个模型？**

它是一个「纯门」（pure gate）：函数签名是 `fn(&Value) -> Result<()>`，成功时返回空的 `Ok(())`，不物化（materialize）任何结构——调用方只关心过不过。这与「解析配置并构建运行时对象」是两件事：后者由第 2 单元的权重清单负责。

它为什么存在？因为服务端启动时要根据 `config.json` 的 `model_type` 把请求分派给正确的模型 crate（`ModelLine` 机制，u1-l1 已讲）。分派的前提是「这个 crate 认领这份配置」，而认领不能只看名字——一个 hidden size 被改过的伪 Kimi 配置必须在这里被拦下，否则后面所有编译期定型的缓冲形状都是错的。

它接受**两种配置身份**：

- 外层 `model_type == "kimi_k25"`：多模态风格的包裹结构，真正的文本模型配置在 `text_config` 子对象里；
- 扁平 `model_type == "kimi_k2"`：整个 JSON 就是文本配置。

无论哪种入口，最终都要求**文本配置自己的 `model_type` 必须是 `"kimi_k2"`**。

#### 4.2.2 核心流程

```text
probe_config_json(json)
  ├─ 1. 读外层 model_type
  │     ├─ "kimi_k25" → text = json.text_config（缺失则报错）
  │     ├─ "kimi_k2"  → text = json 本身
  │     └─ 其他       → bail "not a Kimi-K2 config"
  ├─ 2. text.model_type 必须 == "kimi_k2"
  ├─ 3. 提取 19 个标量字段（缺失即报 "missing … field"）
  ├─ 4. 浮点近似检查：routed_scaling_factor≈2.827、rope_theta≈5e4（容差 1e-6）
  ├─ 5. 路由器语义门：topk_method=="noaux_tc"、scoring_func=="sigmoid"、
  │        norm_topk_prob==true、n_group==1、topk_group==1
  ├─ 6. 维度逐一等于常量（hidden/vocab/layers/first_k_dense/max_pos/heads/
  │        q_lora/kv_lora/nope/rope/v_head/384/top8/shared/2048/18432）
  │        + rms_norm_eps 容差 1e-12
  ├─ 7. rope_scaling 子对象：type=="yarn"、factor≈64、original_pos==4096、
  │        beta_fast≈32、beta_slow≈1、mscale==1.0、mscale_all_dim==1.0
  └─ 8. quantization_config：quant_method=="compressed-tensors"、
           format=="pack-quantized"
  → 全部通过返回 Ok(())
```

注意判定的**顺序性**：字段提取（步骤 3）和语义门（步骤 5）在维度比较（步骤 6）之前。所以一份 `hidden_size` 错误的配置，只有在其余字段齐全、路由器语义正确、浮点接近时，才会精确报出 `hidden_size mismatch`——这一点会被代码实践利用。

浮点比较有两种精度：大多数用 `ensure_float_close`（容差 \(10^{-6}\)，容忍 checkpoint 写出时的十进制舍入），而 `rms_norm_eps`、`mscale`、`mscale_all_dim` 用 \(10^{-12}\) 级别的严格容差——它们必须是精确的 1e-5 / 1.0，因为会逐元素参与数值计算。

#### 4.2.3 源码精读

- [pegainfer-kimi-k2/src/config.rs:43-46](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L43-L46) 文档注释明说这是纯门：调用方只关心成败，不物化任何东西。
- [pegainfer-kimi-k2/src/config.rs:47-55](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L47-L55) 双身份分派：`kimi_k25` 取 `text_config` 子对象，`kimi_k2` 用自身，其他直接 `bail!`。
- [pegainfer-kimi-k2/src/config.rs:57-61](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L57-L61) 内层 `model_type` 必须仍是 `kimi_k2`（防止外层是 kimi_k25 但 text_config 挂羊头）。
- [pegainfer-kimi-k2/src/config.rs:63-81](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L63-L81) 一次性提取全部标量字段；四个小工具函数在 [pegainfer-kimi-k2/src/config.rs:252-277](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L252-L277)，缺字段时报 `missing unsigned integer field <key>` 这类带字段名的错误。
- [pegainfer-kimi-k2/src/config.rs:82-93](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L82-L93) 浮点近似检查（`ensure_float_close` 定义在 [pegainfer-kimi-k2/src/config.rs:279-285](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L279-L285)，容差判断 \( |actual - expected| \le tolerance \)）。
- [pegainfer-kimi-k2/src/config.rs:94-110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L94-L110) 路由器语义门。这不是几何而是**算法合同**：crate 内置的 noaux_tc 路由实现（第 6 单元）只对 sigmoid 打分、无分组、归一化 top-k 的组合给出正确结果，所以这五项任何一项不同都直接拒绝。
- [pegainfer-kimi-k2/src/config.rs:112-179](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L112-L179) 维度逐项 `ensure!` 相等。每个失败分支的文案都是 `<字段名> mismatch: <实际值>`，例如 [pegainfer-kimi-k2/src/config.rs:112-115](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L112-L115) 的 `hidden_size mismatch`。`rms_norm_eps` 在 [pegainfer-kimi-k2/src/config.rs:177-179](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L177-L179) 用 1e-12 严格容差。
- [pegainfer-kimi-k2/src/config.rs:181-227](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L181-L227) `rope_scaling` 子对象全套 YaRN 参数校验（type、factor、original_max_position_embeddings、beta_fast、beta_slow、mscale、mscale_all_dim）。
- [pegainfer-kimi-k2/src/config.rs:229-247](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L229-L247) 量化格式门：`quant_method` 必须是 `compressed-tensors` 且 `format` 必须是 `pack-quantized`——即 crate 只认这一种 INT4 打包格式（第 2 单元会看到它如何被重排成 Marlin 布局）。

上游调用方在 model_line：

- [pegainfer-kimi-k2/src/model_line.rs:56-66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/model_line.rs#L56-L66) `ModelLine::probe` 先做一次宽松的身份识别（外层或 `text_config.model_type` 任一是 kimi 系即可），再把错误转成 `String` 交给服务端调度。`probe_config_json` 本身仍是唯一的事实裁判。

#### 4.2.4 代码实践

**实践目标**：写出能通过纯门的最小合法 `kimi_k2` 配置 JSON，以及一份只改 `hidden_size` 的非法 JSON，并用单测验证「合法通过、非法精确报出 mismatch 字段」。

**操作步骤**：

1. 对照 [pegainfer-kimi-k2/src/config.rs:63-110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L63-L110) 与 [pegainfer-kimi-k2/src/config.rs:112-247](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L112-L247)，手工整理出全部必填字段，写出最小合法 JSON（下方「示例代码 1」已给出，建议先自己写一遍再对照）。
2. 复制一份，只把 `"hidden_size"` 改成 `4096`。
3. 在自己的本地克隆里，临时在 `src/config.rs` 末尾追加一个测试模块（示例代码 2）。这是学习用的本地改动，做完后用 `git checkout -- pegainfer-kimi-k2/src/config.rs` 还原。
4. 运行：

   ```bash
   cargo test -p pegainfer-kimi-k2 --lib config
   ```

   不需要 `--features kimi-k2`：`config` 模块没有 feature 门（[pegainfer-kimi-k2/src/lib.rs:24](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L24)），所以无 GPU 机器也能跑。

**示例代码 1：最小合法配置（手工整理版）**

```json
{
  "model_type": "kimi_k2",
  "hidden_size": 7168,
  "vocab_size": 163840,
  "num_hidden_layers": 61,
  "first_k_dense_replace": 1,
  "max_position_embeddings": 262144,
  "num_attention_heads": 64,
  "q_lora_rank": 1536,
  "kv_lora_rank": 512,
  "qk_nope_head_dim": 128,
  "qk_rope_head_dim": 64,
  "v_head_dim": 128,
  "n_routed_experts": 384,
  "num_experts_per_tok": 8,
  "n_shared_experts": 1,
  "moe_intermediate_size": 2048,
  "intermediate_size": 18432,
  "routed_scaling_factor": 2.827,
  "rms_norm_eps": 1e-5,
  "rope_theta": 50000.0,
  "topk_method": "noaux_tc",
  "scoring_func": "sigmoid",
  "norm_topk_prob": true,
  "n_group": 1,
  "topk_group": 1,
  "rope_scaling": {
    "type": "yarn",
    "factor": 64.0,
    "original_max_position_embeddings": 4096,
    "beta_fast": 32.0,
    "beta_slow": 1.0,
    "mscale": 1.0,
    "mscale_all_dim": 1.0
  },
  "quantization_config": {
    "quant_method": "compressed-tensors",
    "format": "pack-quantized"
  }
}
```

**示例代码 2：追加到 `src/config.rs` 末尾的单测（学习用，测完还原）**

```rust
#[cfg(test)]
mod probe_tests {
    use super::*;

    fn minimal_kimi_k2() -> Value {
        serde_json::json!({
            // ……把示例代码 1 的完整对象原样放进 json! 宏……
        })
    }

    #[test]
    fn probe_accepts_minimal_flat_config() {
        probe_config_json(&minimal_kimi_k2()).unwrap();
    }

    #[test]
    fn probe_rejects_wrong_hidden_size_with_field_name() {
        let mut bad = minimal_kimi_k2();
        bad["hidden_size"] = serde_json::json!(4096);
        let err = probe_config_json(&bad).unwrap_err().to_string();
        assert!(err.contains("hidden_size mismatch"), "got: {err}");
    }
}
```

说明：`use super::*` 会同时引入 `probe_config_json` 与文件顶部的 `serde_json::Value`；`serde_json` 是 crate 的正式依赖（见 [pegainfer-kimi-k2/Cargo.toml:28](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L28)），单测可直接用。

**需要观察的现象**：第一个测试通过；第二个测试失败信息里包含 `hidden_size mismatch: 4096`。

**预期结果**：两个断言都成立。若第二个测试报的却是 `missing … field` 或 `topk_method` 之类的错，说明你的 JSON 还有别的字段问题——这正是 4.2.2 提到的判定顺序在起作用。实际运行结果：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：外层 `model_type` 是 `kimi_k25` 但 JSON 里没有 `text_config` 键，会报什么？

**答案**：`Kimi outer config missing text_config`，来自 [pegainfer-kimi-k2/src/config.rs:49-51](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L49-L51) 的 `ok_or_else`。

**练习 2**：把 `n_group` 改成 8 会发生什么？这说明探针除了几何还管什么？

**答案**：报 `Kimi n_group must be 1`（[pegainfer-kimi-k2/src/config.rs:106-110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L106-L110)）。说明探针同时锁定**路由算法语义**——crate 实现的是无分组的 noaux_tc 路由，分组变体不在支持范围，与其算错不如拒绝。

**练习 3**：为什么「纯门」设计（返回 `Result<()>`、不物化）对这个 crate 特别合适？

**答案**：因为几何已经全部写死在常量里，通过校验的配置不会再提供任何运行时需要的信息——没有「解析出 config 结构体供后续使用」的需求。门的唯一职责是把不认识的 checkpoint 挡在分配显存之前。

### 4.3 `KimiK2ParallelShape` 与 `KimiLocalDims`

#### 4.3.1 概念说明

`KimiK2ParallelShape` 回答第二个问题：**在给定的并行拓扑下，每个 rank（每张 GPU）分到多大的一块模型？**

8 卡只有两种官方拓扑（第 3 单元会讲为什么）：

- `tp8_ep8()`：TP8/DP1——8 卡做张量并行，注意力头、词表都切成 8 份，专家也按 EP8 分布；
- `tp1_dp8()`：TP1/DP8——每卡独立跑完整的一张模型副本（注意力不切、词表不切），只有 384 个专家按 EP8 每卡 48 个分布。

`KimiK2ParallelShape` 保存三个「世界规模」与三个「每 rank 切片宽度」：

\[
\text{heads\_per\_tp} = \frac{64}{tp}, \quad
\text{local\_experts} = \frac{384}{tp \times dp}, \quad
\text{vocab\_per\_tp} = \frac{163840}{tp}
\]

注意 `ep_world = tp_world × dp_world`：两种拓扑的 `ep_world` 都是 8，所以 `local_experts` 都是 48——这是 EP（专家并行）与 TP/DP 正交的直接体现。

`KimiLocalDims` 再往下走一层：由 `heads_per_tp` 和 `tp_world` 推导出**前向计算里真实出现的张量宽度**（q 投影输出多宽、o 投影输入多宽、dense FFN gate_up 多宽……）。第 5 单元的 typed scratch 会把这些数字编进类型系统。

#### 4.3.2 核心流程

```text
KimiK2ParallelShape::new(tp_world, dp_world)
  ep_world     = tp_world × dp_world
  heads_per_tp = 64 / tp_world
  local_experts= 384 / ep_world
  vocab_per_tp = 163840 / tp_world

shape.local_dims()  →  KimiLocalDims（以 h = heads_per_tp 为变量）
  q_proj_out      = h × 192        # q 升投影输出（每头 q 全宽）
  o_proj_in       = h × 128        # o 投影输入（每头 v 输出）
  q_nope_out      = h × 128        # q 的 nope 段
  q_pe_out        = h × 64         # q 的 rope 段
  abs_q_out       = h × 512        # q_nope 吸收后的输出（每头一套 kv_lora）
  dense_gate_up   = 2 × 18432 / tp # dense FFN gate+up 融合宽
  dense_activated = 18432 / tp
  shared_gate_up  = 2 × 2048 / tp  # 共享专家 gate+up 融合宽
  shared_activated= 2048 / tp
```

两种拓扑的手算结果（建议亲手按上面公式复算一遍再对表）：

| 派生量 | `tp8_ep8()`（TP8/DP1） | `tp1_dp8()`（TP1/DP8） |
|---|---|---|
| (tp, dp, ep) | (8, 1, 8) | (1, 8, 8) |
| `heads_per_tp` | 8 | 64 |
| `local_experts` | 48 | 48 |
| `vocab_per_tp` | 20 480 | 163 840（整表） |
| `local_heads` | 8 | 64 |
| `q_proj_out` | 1 536 | 12 288 |
| `o_proj_in` | 1 024 | 8 192 |
| `q_nope_out` | 1 024 | 8 192 |
| `q_pe_out` | 512 | 4 096 |
| `abs_q_out` | 4 096 | 32 768 |
| `dense_gate_up` | 4 608 | 36 864 |
| `dense_activated` | 2 304 | 18 432 |
| `shared_gate_up` | 512 | 4 096 |
| `shared_activated` | 256 | 2 048 |

两个值得单独指出的点：

1. **`tp1_dp8` 下 `vocab_per_tp` 是全量 163840**：DP 不切参数，每卡持有完整词表的 embedding 与 lm_head。
2. **一个危险的数字巧合**：`tp8_ep8` 的 `q_proj_out`（\( 8 \times 192 = 1536 \)）在数值上恰好等于全局常量 `KIMI_K2_Q_LORA_RANK`（1536）。两者语义完全无关（一个是「8 头 × 每头 192」，一个是「Q 压缩秩」），读源码时不要把它们混为一谈。

#### 4.3.3 源码精读

- [pegainfer-kimi-k2/src/config.rs:287-295](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L287-L295) `KimiK2ParallelShape` 结构体：三个 world 字段 + 三个切片宽度字段。
- [pegainfer-kimi-k2/src/config.rs:297-311](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L297-L311) `KimiLocalDims` 结构体，文档注释点明「所有字段随 `heads_per_tp` 或 `tp_world` 缩放」。
- [pegainfer-kimi-k2/src/config.rs:313-322](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L313-L322) 两个官方构造器 `tp8_ep8()` 与 `tp1_dp8()`，只是 `new(8,1)` / `new(1,8)` 的具名别名。
- [pegainfer-kimi-k2/src/config.rs:324-335](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L324-L335) `new` 里的三条除法就是 4.3.2 的三个公式；`ep_world = tp_world * dp_world` 也在这里。
- [pegainfer-kimi-k2/src/config.rs:337-352](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L337-L352) `local_dims()`：以 `h = heads_per_tp` 逐字段乘出 `KimiLocalDims`。
- [pegainfer-kimi-k2/src/config.rs:354-357](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L354-L357) `parallel_config()` 把 shape 转成 frontend 的通用 `ParallelConfig`——这是模型 crate 与并行运行时之间的桥。

三个消费点（本讲只定位，不深挖）：

- [pegainfer-kimi-k2/src/runner/bringup.rs:70-78](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L70-L78) 启动时按 `(tp_world, dp_world)` 精确匹配 `(8,1)` / `(1,8)` 两条路径，其余拓扑直接 `bail!`「not yet supported」。
- [pegainfer-kimi-k2/src/runner/bringup.rs:96-97](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L96-L97) 权重清单用 `with_parallel_shape(shape)` 绑定拓扑；[pegainfer-kimi-k2/src/runner/bringup.rs:111](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L111) 把 `shape.local_dims()` 塞进运行时配置。
- [pegainfer-kimi-k2/src/weights/manifest.rs:284-289](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L284-L289) `rank_plan` 用 `heads_per_tp` / `vocab_per_tp` / `local_experts` 乘上 tp_rank / ep_rank 得到每个 rank 的头区间、词表区间、专家区间——第 2 单元的主角；其前置的整除校验在 [pegainfer-kimi-k2/src/weights/manifest.rs:265-271](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L265-L271)（`is_multiple_of` 检查，不整除即拒绝）。

#### 4.3.4 代码实践

**实践目标**：不看讲义表格，手算两种拓扑的全部派生量，再用单测让编译器批改你的作业。

**操作步骤**：

1. 拿一张纸，按 4.3.2 的公式对 `tp8_ep8` 与 `tp1_dp8` 各算一遍 13 个派生量。
2. 与讲义中表格对答案；错的那格回到 [pegainfer-kimi-k2/src/config.rs:324-352](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L324-L352) 找原因。
3. （可选，加深记忆）沿用 4.2.4 的本地测试模块，追加两个断言测试（示例代码 3，仍属学习用临时改动）：

   ```rust
   #[test]
   fn tp8_ep8_slices() {
       let s = KimiK2ParallelShape::tp8_ep8();
       assert_eq!(s.ep_world, 8);
       assert_eq!(s.heads_per_tp, 8);
       assert_eq!(s.local_experts, 48);
       assert_eq!(s.vocab_per_tp, 20_480);
       assert_eq!(s.local_dims().abs_q_out, 4_096);
   }

   #[test]
   fn tp1_dp8_keeps_full_heads_and_vocab() {
       let s = KimiK2ParallelShape::tp1_dp8();
       assert_eq!(s.heads_per_tp, 64);
       assert_eq!(s.vocab_per_tp, 163_840);
       assert_eq!(s.local_experts, 48); // EP 与 TP/DP 正交：仍是 384/8
   }
   ```

   注意 `KimiLocalDims` 只派生 `Clone, Copy, Debug`（[pegainfer-kimi-k2/src/config.rs:299](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L299)），没有 `PartialEq`，所以只能逐字段断言，不能整个结构体比较。
4. 运行 `cargo test -p pegainfer-kimi-k2 --lib`（依旧无需 feature、无需 GPU），完成后还原 `src/config.rs`。

**需要观察的现象**：两个测试全绿；若把 `local_experts` 的断言写成 384 或把 `tp1_dp8` 的 `vocab_per_tp` 写成 20480，测试立刻红。

**预期结果**：手算表与断言一致。实际运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么两种拓扑的 `local_experts` 相同，而 `heads_per_tp` 差 8 倍？

**答案**：`local_experts` 除以的是 `ep_world`（两种拓扑都是 8），专家怎么摆只看 EP；`heads_per_tp` 除以的是 `tp_world`（8 vs 1），注意力头只有 TP 会切。EP 与 TP/DP 是正交的两个轴。

**练习 2**：如果有人想跑 TP4/DP2（`new(4, 2)`），`new()` 本身会算出什么？实际会发生什么？

**答案**：`new(4,2)` 会算出 ep_world=8、heads_per_tp=16、local_experts=48、vocab_per_tp=40960——算术上全部整除、毫无障碍；但启动路径在 [pegainfer-kimi-k2/src/runner/bringup.rs:73-77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L73-L77) 只认 `(8,1)` 与 `(1,8)`，TP4/DP2 会直接 `bail!` "not yet supported"。形状层是纯算术，支持范围由启动层决定。

**练习 3**：`abs_q_out = h × KIMI_K2_KV_LORA_RANK` 里为什么乘的是 KV 的压缩秩而不是 Q 自己的维数？

**答案**：这是 MLA 的「q_nope 吸收」技巧（第 5 单元细讲）：q 的 nope 段不显式升维到 128，而是与上投影矩阵 \( W_{UK} \) 融合，直接在 512 维潜在空间里做点积，所以每个头吸收后的输出宽度是 `kv_lora_rank`（512）而不是 nope 维（128）。字段名里的 `abs` 就是 absorb。

## 5. 综合实践

**任务：给你的「几何速查卡」做一次三方对账。**

1. 从 [pegainfer-kimi-k2/src/config.rs:13-41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L13-L41) 抄出全部常量，做成一张速查卡（分组：整体尺寸 / MLA / MoE / 量化 / YaRN）。
2. 用速查卡上的数字**从零**重写 4.2.4 的最小合法 JSON——只允许看常量与 [pegainfer-kimi-k2/src/config.rs:63-247](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L63-L247) 的字段名，不许回看示例代码 1。
3. 跑 `cargo test -p pegainfer-kimi-k2 --lib config` 验证（待本地验证）。
4. 再把速查卡上的 hidden / heads / experts / vocab 四个数代进 4.3.2 的三个公式，手算两种拓扑的切片宽度，与 `local_dims()` 的代码逐字段核对。
5. 最后做一次「破坏实验」：任选三个字段（一个几何、一个路由语义、一个 rope_scaling 内层）分别改错，预测各自的报错文案，再跑探针验证预测。全对说明你已经能在脑子里执行这个函数了。

## 6. 本讲小结

- Kimi-K2.6 的全部几何写死在 `config.rs` 的 `KIMI_K2_*` 常量里：hidden 7168、64 头（每头 q 192 = nope 128 + rope 64、v 128）、q_lora 1536、kv_lora 512、61 层 = 1 dense + 60 MoE、384 专家 top-8 + 1 共享、INT4 组 32、YaRN 4096×64=262144。
- `probe_config_json` 是纯门：只回答「是不是这个模型」，不物化任何结构；接受 `kimi_k25`（外层 + `text_config`）与扁平 `kimi_k2` 两种身份，内层 `model_type` 必须是 `kimi_k2`。
- 探针不只锁几何，还锁路由算法语义（noaux_tc / sigmoid / norm_topk_prob / n_group=1）与量化格式（compressed-tensors + pack-quantized）。
- `KimiK2ParallelShape::tp8_ep8` / `tp1_dp8` 用三条除法推导每 rank 切片：heads 64/tp、experts 384/(tp×dp)、vocab 163840/tp；`local_dims()` 进一步给出前向张量的真实宽度。
- 常量与形状层刻意不挂 feature 门：无 GPU、无 `kimi-k2` feature 也能 `cargo check` / 跑单测，这让本讲的全部实践在任何机器上可做。
- 这些派生宽度不是纸上谈兵——`bringup.rs` 与 `weights/manifest.rs` 的 rank 区间切分（第 2、3 单元）直接消费它们。

## 7. 下一步学习建议

- 下一讲（u1-l3「crate 模块地图与 feature 门」）会展开 `lib.rs` 的完整模块树，看清 `config` 在整个 crate 里的邻居关系；随后 u1-l4 讲怎么把引擎真正跑起来。
- 想提前看派生形状的「重头戏」，可以直接读 [pegainfer-kimi-k2/src/weights/manifest.rs:275-299](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L275-L299) 的 `rank_plan`，体会 `heads_per_tp` / `vocab_per_tp` / `local_experts` 如何变成具体的张量区间——这是第 2 单元的核心预习材料。
- 对 MLA 各维数的几何直觉（为什么 1536/512/128/64/128 长这样）在第 5 单元 u5-l2 会随源码完整展开，本讲记住数字与角色即可。
