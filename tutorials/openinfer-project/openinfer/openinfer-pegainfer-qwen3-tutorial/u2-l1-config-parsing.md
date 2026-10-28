# 配置解析：从 config.json 到 Config

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `probe_config_json` 的身份检查与 `Config::from_file` 的完整校验各自负责什么、在启动链的哪个时机执行、为什么这样分层。
2. 解释 `Config` 结构体如何用 serde 直接派生自 HuggingFace 的 `config.json`，哪些字段来自 JSON、哪些字段是 `#[serde(skip)]` 的程序填充值。
3. 理解 `TensorParallelConfig` 如何校验并推导张量并行（TP）切分——它检查的是注意力头数、KV 头数与中间维度，而**不是层数**。
4. 跟踪 `Raw*` 中间结构（`RawDFlashConfig` → `DFlashGeometrySource` → `DFlashGeometry`）归一化到最终 `DFlashConfig` 的路径，并找到投机解码配置（`DFlashConfig` / `Eagle3Config`）的加载入口。
5. 亲手为「非法配置必须被拒绝」写出一组单测。

本讲只看**静态配置**：模型目录里那份 `config.json` 如何变成内存里一个被校验过的 `Config` 值。GPU、权重上传、调度都不在本讲范围。

## 2. 前置知识

**HuggingFace 模型目录长什么样。** 一个 Qwen3 checkpoint 目录大致是：

```text
models/Qwen3-4B/
├── config.json            # 本讲的主角：模型几何与超参
├── model-00001-of-00002.safetensors   # 权重分片（u2-l3 讲装载）
├── model.safetensors.index.json
├── tokenizer.json / tokenizer_config.json / generation_config.json
└── ...
```

`config.json` 是一个 JSON 对象，字段名沿用 transformers 的 `Qwen3Config` 命名（`hidden_size`、`num_attention_heads`……），所以本 crate 的解析器天然兼容 HF 生态导出的模型。

**serde 的 `Deserialize` 是什么。** serde 是 Rust 的事实标准序列化框架。给结构体标上 `#[derive(Deserialize)]`，serde 就会生成「JSON 字段 → 结构体字段」的逐一同名映射代码：字段缺失报 `missing field`，类型不符报类型错误，多余字段默认忽略。它还支持 `#[serde(default)]`（缺失时填默认值）、`#[serde(skip)]`（该字段不参与反序列化，由代码手工填充）等属性——本讲会反复遇到这三种。

**GQA（Grouped Query Attention，分组查询注意力）。** 解码时每个 token 要用 \( H_q \) 个查询头去读 KV 缓存。若 KV 头数与查询头数相同，缓存太大；GQA 让多个查询头共享一组 KV：KV 头数 \( H_{kv} \) 更小。这要求

\[ g = \frac{H_q}{H_{kv}} \]

是正整数（每个 KV 头服务 \( g \) 个查询头）。Qwen3-4B/8B 都是 \( H_q = 32 \)、\( H_{kv} = 8 \)，即 \( g = 4 \)。

**张量并行（TP）与流水线并行（PP）的区别。** TP 把**同一层内部**的矩阵按行/列切到多张卡：注意力按头切、MLP 按中间维度切，每张卡算自己那一份再通信归约。PP 才是**按层切**（第 0–17 层在卡 0，第 18–35 层在卡 1）。pegainfer-qwen3 只做 TP，所以你会看到 `TensorParallelConfig::validate_for` 校验的是头数和中间维度的可除性——`num_hidden_layers` 从头到尾不参与 TP 校验。

**失败越早越便宜。** 承接 u1-l3 的结论：clap 解析期拒绝（最便宜）→ `probe` 身份检查 → `validate` 跨标志互斥 → 装载期语义校验（本讲）→ 运行期才暴露（最昂贵）。本讲就是在「装载期」这一阶梯上，把坏配置拦在分配显存之前。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/config.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs) | 本讲主体：`Config`、`TensorParallelConfig`、`probe_config_json`、`DFlashConfig`/`Eagle3Config` 及其 `Raw*` 中间结构，全部配置解析与校验都在这一个文件 |
| [src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs) | `Qwen3Line::probe` 是 `probe_config_json` 的唯一上游调用点；其 `tests` 模块是本讲实践要仿照的测试组织范本（u1-l3 已讲过 CLI 侧） |
| [src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs) | `Config::from_file` 与 `tensor_parallel.validate_for` 的真实消费点（checkpoint 装载入口，u2-l3 详讲） |
| [src/executor.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs) | `TensorParallelConfig { rank, world_size }` 的构造来源，以及 DFlash 草稿配置的加载入口 |
| [pegainfer-kernels/src/ops/attention.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kernels/src/ops/attention.rs) | `SUPPORTED_GQA_GROUP_SIZES` 常量：哪些 GQA 比值有编译好的解码内核 |
| [pegainfer-core/src/eos.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/eos.rs) | `eos_token_ids`：从 tokenizer 读停止符 id，是 `from_file` 的最后一步 |

## 4. 核心概念与源码讲解

### 4.1 probe_config_json：身份证检查

#### 4.1.1 概念说明

服务器收到 `--model-path` 后，第一件事是问：这个目录是**哪条模型线**的？它读出 `config.json` 的 JSON 值，依次调用每条模型线的 `probe` hook。probe 只做**身份检查**（identity check）：确认「这是一份 Qwen3 的配置」，而不管尺寸、形状是否合法。

为什么不在 probe 里顺便校验几何？因为职责分离：probe 运行在**服务器派发阶段**，此刻还不知道由谁装载，只需要回答「是不是我的」；尺寸与形状校验属于配置装载器，那里的错误信息可以带上完整的上下文（哪个字段、期望什么）。源码的文档注释把这层契约写得很明确。

#### 4.1.2 核心流程

```text
服务器读 --model-path/config.json 为 serde_json::Value
        │
        ▼ 对每条模型线的 MODEL_LINE.probe(json)
Qwen3Line::probe (model_line.rs)
        │  ① 先自查 model_type == "qwen3"（给 CLI 的错误文案）
        │  ② 委托 crate::probe_config_json
        ▼
probe_config_json (config.rs)
        │  ③ 再查 model_type == "qwen3"
        │  ④ 查 architectures 数组包含 "Qwen3ForCausalLM"
        ├── 任一不满足 → Err（服务器换下一条模型线试）
        └── 全过 → Ok(())，服务器选定 Qwen3 线，进入 augment_cli / validate / launch
```

注意 probe 失败对服务器而言通常不是致命错误——它只是说明「这不是 Qwen3」，换下一条线匹配即可。真正致命的是：所有线都探测失败。

#### 4.1.3 源码精读

身份检查本体只有十几行：

- [src/config.rs:895-911](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L895-L911) — `probe_config_json`：文档注释声明「身份检查归 probe，尺寸与形状校验归配置装载器」；`model_type` 缺失或不是 `"qwen3"` 就 `bail!`；`architectures` 缺失时 `unwrap_or_default()` 得到空数组，`ensure!` 随之失败——所以「没有 architectures 字段」与「architectures 里没有 `Qwen3ForCausalLM`」都会被拒绝。

上游接缝在 ModelLine（u1-l3 讲过 trait 全貌，这里只看 probe）：

- [src/model_line.rs:129-135](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L129-L135) — `Qwen3Line::probe` 先自己比对一次 `model_type`（产出面向 CLI 的友好文案），再把 `anyhow::Error` 转成 `String` 返回给前端契约。

现有测试已经把三种身份结论钉住了，这是本讲实践要仿照的组织方式：

- [src/model_line.rs:395-412](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L395-L412) — 三个测试：接受 `qwen3 + Qwen3ForCausalLM`；拒绝错误的 `architectures`；拒绝外来 `model_type`（并断言错误信息包含 `qwen3`）。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认 probe 对「字段缺失」与「字段错误」一视同仁地拒绝，并理解 `unwrap_or_default` 的作用。
2. **操作步骤**：在 `src/model_line.rs` 的 `tests` 模块（[382-393 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L382-L393) 的 `validate_argv` 下方）补一个测试（示例代码，需你亲手加入源码的测试模块）：

   ```rust
   #[test]
   fn probe_rejects_missing_architectures() {
       let json = serde_json::json!({"model_type": "qwen3"});
       MODEL_LINE.probe(&json).unwrap_err();
   }
   ```

   然后运行：

   ```bash
   cargo test --release -p pegainfer-qwen3 --lib probe_
   ```

3. **需要观察的现象**：新测试与既有三个 probe 测试全部通过；`unwrap_err()` 没有 panic。
4. **预期结果**：通过。依据是 [src/config.rs:901-909](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L901-L909)：`architectures` 键缺失时 `.and_then(Value::as_array)` 返回 `None`，`.unwrap_or_default()` 得空 `Vec`，`ensure!(architectures.contains(...))` 必然失败。本环境的运行输出为**待本地验证**（编译本 crate 需要 CUDA 工具链）。

#### 4.1.5 小练习与答案

1. **`{"model_type": "qwen3"}`（完全没有 `architectures`）会被 probe 接受吗？**
   答：不会。`architectures` 缺失经 `unwrap_or_default()` 变成空数组，`ensure!` 失败，报 "Qwen3 architectures must contain Qwen3ForCausalLM"。
2. **`Qwen3Line::probe` 里 `model_type` 检查了两次（外层一次、`probe_config_json` 里又一次），是 bug 吗？**
   答：不是。外层（model_line.rs:130-133）把检查结果格式化成面向 CLI 派发的 `String` 错误；内层是 crate 级的身份不变量，供 crate 内其他调用方复用。两层语义相同、报错出口不同。
3. **为什么不把「`num_attention_heads` 必须是 `num_key_value_heads` 的倍数」也放进 probe？**
   答：那是几何校验，不是身份判断。probe 阶段只需要区分模型线；几何错误应在装载器里报出带完整上下文的错误，且此时失败也更便宜（还没建 CUDA 上下文、没读权重）。

### 4.2 Config 解析与校验：serde 派生 + from_file 语义检查

#### 4.2.1 概念说明

`Config` 是 Qwen3 主模型的**几何真值**：隐藏宽度、层数、头数、词表大小、RoPE 参数……后续一切（权重分片、KV 池尺寸、内核选择、调度上限）都从它推导。它身上体现了一个重要的设计取舍：

- **能交给 serde 的就交给 serde**：字段同名映射、缺字段/错类型由派生代码统一处理，零手写。
- **serde 做不了语义的才手写**：GQA 整倍数关系、停止符读取这类跨字段的检查放在 `from_file`。
- **依赖装载上下文的延后处理**：`decodable_vocab`/`logits_rows` 在 `from_file` 先取保守初值，装载时再按 tokenizer 收敛。

#### 4.2.2 核心流程

```text
Config::from_file(model_path)
  ① fs::read_to_string(model_path + "/config.json")
  ② serde_json::from_str::<Config>          ← 缺字段/类型错在这里失败
  ③ decodable_vocab = logits_rows = vocab_size   ← 保守初值（#[serde(skip)] 字段）
  ④ ensure: num_key_value_heads > 0 且 H_q 是 H_kv 的正整数倍   ← 硬失败
  ⑤ decode_group_is_compiled()? 否则 log::warn   ← 软降级，不失败
  ⑥ eos_token_ids(model_path, vocab_size)    ← 读 tokenizer，越界即失败
  → Ok(Config)
```

注意 ④ 在 ⑥ 之前：GQA 违规的配置在触碰 tokenizer 之前就被拒绝——这个顺序是 4.2.4 实践的立足点。

#### 4.2.3 源码精读

先看结构体与字段的三个类别：

- [src/config.rs:27-49](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L27-L49) — `Config` 直接 `derive(Deserialize)`。字段分三类：
  - **来自 JSON**：`hidden_size`、`intermediate_size`、`num_hidden_layers`、`num_attention_heads`、`num_key_value_heads`、`head_dim`、`vocab_size`、`rms_norm_eps`、`rope_theta`、`tie_word_embeddings`；
  - **带默认值**：`max_position_embeddings` 缺省补 40960（[300-302 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L300-L302) 的 `default_max_position_embeddings`）；
  - **`#[serde(skip)]` 程序填充**：`decodable_vocab`（token 选择宽度）、`logits_rows`（对齐后的输出投影行数）、`stop_token_ids`（停止符）。
- 另一个值得注意的 serde 细节：`Config` **没有** `#[serde(deny_unknown_fields)]`。HF 的 `config.json` 常带 `architectures`、`torch_dtype`、`auto_map` 等杂项，忽略未知字段是对 HF 生态的必要兼容。反例见 [src/config.rs:155-161](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L155-L161) 的 `RawDSpineSecondPass`——那是内部协议字段，拼错就该立刻暴露，所以显式 `deny_unknown_fields`。

再看 `from_file` 的三段校验：

- [src/config.rs:305-330](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L305-L330) — `from_file` 全文：读文件 → serde 解析 → 填初值 → GQA `ensure!` → 内核覆盖警告 → 读 eos。
- [src/config.rs:311-319](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L311-L319) — 硬校验：\( H_q \bmod H_{kv} = 0 \) 且 \( H_{kv} > 0 \)，错误信息带两个实际值。
- [src/config.rs:320-336](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L320-L336) — 软降级：GQA 比值不在编译表里时只 `log::warn!`——解码将 eager 地走 prefill 注意力路径、CUDA Graph 解码被禁、`--decode-overlap` 不可用。这是「能力降级」而非「配置非法」，所以不失败。比值守表来自 [pegainfer-kernels/src/ops/attention.rs:21](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kernels/src/ops/attention.rs#L21)：`&[1, 2, 3, 4, 6, 8]`。
- [src/config.rs:328](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L328) — 停止符来自 tokenizer（[pegainfer-core/src/eos.rs:18](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/eos.rs#L18)：空列表、非 u32、超出 `vocab_size` 的 id 都会报错）。

`#[serde(skip)]` 字段的初值随后在装载期被收敛——这是「配置解析」与「checkpoint 装载」的接力点：

- [src/weights/load.rs:76-88](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L76-L88) — 装载入口重新审视 `decodable_vocab`：tokenizer 实际可解码的词表不得超过 checkpoint 的 `vocab_size`；`logits_rows` 取对齐后的行数（超出 `decodable_vocab` 的行在采样时被抑制）。细节属于 u2-l2/u2-l3。

辅助方法一览（本讲只点名，消费场景在后续讲）：

- [src/config.rs:338-344](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L338-L344) — `lm_head_tensor_name`：`tie_word_embeddings = true` 时输出投影直接复用 embedding 权重张量名。

#### 4.2.4 代码实践

本讲的主实践（对应任务书里的「为 Config 加载补一个拒绝非法配置的单测」）分两步，都放进 `src/config.rs` 底部既有的 `#[cfg(test)] mod tests`（[913 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L913) 起），组织方式仿照 model_line.rs 的测试：描述性函数名 + `expect_err` + 断言错误文本包含关键词。

**实践一：缺失关键字段被 serde 拒绝。**

```rust
#[test]
fn config_requires_head_dim() {
    let json = serde_json::json!({
        "hidden_size": 2560, "intermediate_size": 9728, "num_hidden_layers": 36,
        "num_attention_heads": 32, "num_key_value_heads": 8,
        "vocab_size": 151936, "rms_norm_eps": 1e-6, "rope_theta": 1000000.0,
        "tie_word_embeddings": true
        // 故意不写 head_dim
    });
    let error = serde_json::from_str::<Config>(&json.to_string())
        .expect_err("missing head_dim must fail serde");
    assert!(error.to_string().contains("missing field"), "{error}");
}
```

**实践二：GQA 违规在触碰 tokenizer 之前被 `from_file` 拒绝。** 因为 `ensure!`（311 行）先于 `eos_token_ids`（328 行），所以临时目录里**只需要** `config.json`，不需要 tokenizer 文件：

```rust
#[test]
fn from_file_rejects_non_multiple_gqa_before_touching_tokenizer() {
    let json = serde_json::json!({
        "hidden_size": 2560, "intermediate_size": 9728, "num_hidden_layers": 36,
        "num_attention_heads": 31, "num_key_value_heads": 8, "head_dim": 128,
        "vocab_size": 151936, "rms_norm_eps": 1e-6, "rope_theta": 1000000.0,
        "tie_word_embeddings": true
    });
    let directory = tempfile::tempdir().expect("temp dir");
    fs::write(directory.path().join("config.json"), json.to_string()).expect("write config");

    let error = Config::from_file(directory.path().to_str().unwrap())
        .expect_err("31 query heads over 8 kv heads must be rejected");
    assert!(
        error.to_string().contains("must be a positive multiple"),
        "unexpected error: {error:#}"
    );
}
```

（以上两段为示例代码——写进测试模块的实践代码，非项目原有内容。）

1. **实践目标**：验证「缺字段由 serde 拦、语义违规由 `from_file` 拦」的分层，并体会校验顺序带来的可测性。
2. **操作步骤**：把两个测试加入 `src/config.rs` 的 `tests` 模块（该模块已有 `use super::*`，`fs`、`tempfile`、`serde_json::json!` 都是现成依赖，参照 [935-939 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L935-L939) 的既有写法）；运行：

   ```bash
   cargo test --release -p pegainfer-qwen3 --lib config::tests
   ```

3. **需要观察的现象**：两个新测试通过；如果把实践二里的 `num_attention_heads` 改回 32，错误会变成 eos/tokenizer 相关（目录里没有 tokenizer 文件）——这恰好证明校验顺序。
4. **预期结果**：按源码推导应当通过（错误文案分别来自 serde 的 `missing field` 与 [316 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L316) 的 `must be a positive multiple`）。运行输出**待本地验证**。

#### 4.2.5 小练习与答案

1. **JSON 里漏写 `max_position_embeddings` 会失败吗？**
   答：不会。`#[serde(default = "default_max_position_embeddings")]`（[44-45 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L44-L45)）把缺失值补成 40960。
2. **一个 GQA 比值为 5 的假想模型（如 40 头 / 8 KV 头）能通过 `from_file` 吗？**
   答：能通过（5 是正整数倍），但比值 5 不在 `SUPPORTED_GQA_GROUP_SIZES` 的 `[1,2,3,4,6,8]` 里，于是触发 `log::warn!` 软降级：解码走 eager prefill 路径、CUDA Graph 解码禁用、`--decode-overlap` 不可用。
3. **`decodable_vocab` 为什么不在 serde 解析时直接确定？**
   答：它依赖 tokenizer（另一个文件），而 `Config` 只派生自 `config.json`。`from_file` 先填保守初值 `vocab_size`，真正的收敛发生在装载入口 [weights/load.rs:77-88](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L77-L88)——解析与装载各司其职。

### 4.3 TensorParallelConfig：切分推导与可除性校验

#### 4.3.1 概念说明

`TensorParallelConfig` 只有两个字段：`rank`（我是第几号卡）和 `world_size`（一共几张卡参与 TP）。它回答的问题是：**在层内，这张卡负责哪一段？**

TP 对 Qwen3 的三类维度做均匀行切：

| 维度 | 全局值（8B 为例） | TP=2 时每 rank | 切的是什么 |
|------|------|------|------|
| `num_attention_heads` | 32 | 16 | 注意力查询头 |
| `num_key_value_heads` | 8 | 4 | KV 头（连带 KV 缓存宽度） |
| `intermediate_size` | 12288 | 6144 | MLP gate/up/down 的中间维 |
| `num_hidden_layers` | 36 | 36（**不切**） | 层属于流水线并行的切分对象，本 crate 不做 PP |

`num_hidden_layers` 不被 TP 校验、也不被切分——任务书里「层数不能被 TP 整除」这个说法并不对应任何真实检查；真实的拒绝条件是**头数或中间维度不能被 world_size 整除**。

#### 4.3.2 核心流程

校验（`validate_for`）的五道关卡，任一不过即 `Err`：

```text
① world_size >= 1
② rank < world_size
③ num_attention_heads  ≡ 0 (mod world_size)
④ num_key_value_heads   ≡ 0 (mod world_size)
⑤ intermediate_size     ≡ 0 (mod world_size)
```

切分推导是一组纯函数。设全局总量 \( T \)、并行度 \( W \)、rank \( r \)，可除性已由校验保证：

\[ \text{shard\_len} = T / W, \qquad \text{rank } r \text{ 拥有行 } [\,r \cdot \text{shard\_len},\ (r+1) \cdot \text{shard\_len}\,) \]

每 rank 的查询/KV 投影输出宽度：

\[ \text{local\_q\_dim} = \frac{H_q}{W} \times d_{head}, \qquad \text{local\_kv\_dim} = \frac{H_{kv}}{W} \times d_{head} \]

GQA 比值 \( g = H_q / H_{kv} \) 在 TP 下不变（分子分母同除 \( W \)），所以「比值是否落在内核支持表」这一判断对全局与每 rank 一致——源码注释称之为 TP-invariant。

#### 4.3.3 源码精读

- [src/config.rs:12-25](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L12-L25) — 结构体与默认值：`rank: 0, world_size: 1`，即「单卡、零号卡」就是退化情形，这让所有下游代码可以无条件地拿着一个 TP 配置算切分。
- [src/config.rs:850-883](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L850-L883) — `validate_for`：五道检查按上面的顺序排列，每条错误信息都带实际值（如 `"num_attention_heads=32 not divisible by tp world_size=3"`）。注意没有任何一行提到 `num_hidden_layers`。
- [src/config.rs:885-892](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L885-L892) — `shard_range(total)` 返回 `(起点, 长度)` 二元组而非 `(起, 止)`；`is_sharded()` 就是 `world_size > 1`。
- [src/config.rs:346-364](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L346-L364) — `local_*` 辅助函数族：`local_num_attention_heads` / `local_num_key_value_heads` / `local_intermediate_size` / `local_q_dim` / `local_kv_dim`，全部是「全局值除以 world_size」的一行式。
- [src/config.rs:332-336](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L332-L336) — `decode_group_is_compiled` 的文档注释点名 GQA 比值的 TP 不变性。

两个真实调用点，回答「这个配置从哪来、到哪被校验」：

- [src/executor.rs:1398-1416](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1398-L1416) — 构造来源：`world_size = device_ordinals.len()`（选了几张卡），`rank` 是卡列表的枚举序号；每个 rank 各自走一遍 `Qwen3Model::from_safetensors_with_runtime`。
- [src/weights/load.rs:101-102](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L101-L102) — 校验时机：装载入口读完 `Config` 后立刻 `tensor_parallel.validate_for(&config)`，**先于**任何权重上传与显存预留——坏 TP 配置在最便宜的时刻失败。

#### 4.3.4 代码实践

1. **实践目标**：为「TP 不可整除即拒绝」补单测（任务书指定实践的第二种非法形态），并用 Qwen3-8B 的真实几何手推一张 TP=2 切分表。
2. **操作步骤**：在 `src/config.rs` 的 `tests` 模块里仿照 [1044-1061 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L1044-L1061) 既有的 `target_qwen3_8b()` 夹具（结构体字面量直接构造 `Config`，不走 serde）加测试（示例代码）：

   ```rust
   #[test]
   fn tensor_parallel_rejects_indivisible_heads() {
       let config = target_qwen3_8b(); // 32 头 / 8 KV 头 / intermediate 12288
       let error = TensorParallelConfig { rank: 0, world_size: 3 }
           .validate_for(&config)
           .expect_err("32 heads cannot shard across 3 ranks");
       assert!(
           error.to_string().contains("num_attention_heads=32 not divisible by tp world_size=3"),
           "unexpected error: {error:#}"
       );
   }

   #[test]
   fn tensor_parallel_rejects_rank_out_of_range() {
       let config = target_qwen3_8b();
       TensorParallelConfig { rank: 2, world_size: 2 }
           .validate_for(&config)
           .expect_err("rank must stay below world_size");
   }
   ```

   运行：

   ```bash
   cargo test --release -p pegainfer-qwen3 --lib tensor_parallel
   ```

3. **需要观察的现象**：两个测试通过；再把 `world_size` 换成 2，第一个测试的 `expect_err` 会 panic（因为 32/8/12288 对 2 全部可整除，校验放行）——这验证了通过路径。
4. **预期结果**：错误文案逐字来自 [861-867 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L861-L867) 与 [854-860 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L854-L860)。运行输出**待本地验证**。
5. **手推作业**：对 8B（32 头 / 8 KV 头 / 12288 / head_dim 128）填表——TP=2 应得 local 头 16、local KV 头 4、local intermediate 6144、`local_q_dim` = 16×128 = 2048、`local_kv_dim` = 4×128 = 512、GQA 比值仍为 4。

#### 4.3.5 小练习与答案

1. **Qwen3-8B 在 world_size=3 下会挂在哪一条检查？**
   答：第③条：32 不能被 3 整除，报 `num_attention_heads=32 not divisible by tp world_size=3`（若头数恰好可整除，还会继续撞上 8 % 3 ≠ 0 的第④条）。
2. **`shard_range(12288)` 在 world_size=2、rank=1 时返回什么？**
   答：`shard_len = 6144`，返回 `(6144, 6144)`——rank 1 拥有中间矩阵的后 6144 行。
3. **为什么 `validate_for` 不检查 `num_hidden_layers`？**
   答：TP 切的是层内宽度（头、中间维），不是层；按层切分属于流水线并行，pegainfer-qwen3 没有实现 PP。36 层在 TP=2 下每张卡仍是完整 36 层。

### 4.4 Raw* 归一化与投机解码配置入口

#### 4.4.1 概念说明

主模型的 `Config` 之所以能「一步 serde」，是因为 HF 的 `Qwen3Config` 只有一种 schema。投机解码的草稿模型（DFlash/DSpark/DFlash2）没有这种福气——它们的 `config.json` 有**三种**历史布局：字段平铺的（flat）、嵌在 `dflash_config` 子对象里的（b16）、几何嵌在 `transformer_layer_config` 里的（Speculators）。于是 config.rs 采用「Raw 结构宽容吸收、最终结构严格唯一」的两段式：

- `Raw*` 结构只管「把三种形态都吃进来」，字段几乎全是 `Option`；
- `from_json` 把 `Raw*` 逐字段归一化成唯一的 `DFlashConfig`，缺关键字段在此报错；
- `validate_for_target` 再做草稿↔目标的跨配置校验。

`Eagle3Config` 是对照组：单一 schema，直接 `derive(Deserialize)`，不需要 Raw 层。`layer_types`（层类型表）的缺省补全逻辑也只活在草稿器这一侧——主模型 `Config` 根本不读这个字段（Qwen3 稠密模型全是 full attention）。

#### 4.4.2 核心流程

```text
DFlashConfig::from_file(model_path)
  └─ from_json(content)
       ① serde → RawDFlashConfig（宽容：字段皆 Option/默认值）
       ② 几何抽取：DFlashGeometrySource::Speculators │ Flat → DFlashGeometry
       ③ 字段级联：dflash_config.* 优先，顶层兜底（block_size、selector、conv…）
       ④ rope_theta：平铺 或 rope_parameters.rope_theta
       ⑤ target_layer_ids：嵌套直取；平铺时 target_layer_ids/aux_hidden_state_layer_ids
          （后者逐项 -1：Speculators 的 0 号是 embedding，本 crate 用 0-based 解码器输出）
       ⑥ DSpine 桥检查（layer_types 显式全 full 或缺省补全后无 sliding）
       ⑦ 组装唯一 DFlashConfig（anchor_first = num_anchors 存在 或 markov_rank > 0）

Eagle3Config::from_file → 直接 serde（无 Raw 层）
两者随后：validate_for_target(&目标 Config) —— 跨配置一致性
```

#### 4.4.3 源码精读

- [src/config.rs:219-226](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L219-L226) — `DFlashGeometrySource` 是 `#[serde(untagged)]` 枚举：先尝试 Speculators 形态（几何在 `transformer_layer_config` 下），失败再按 Flat 解析。几何本体 [185-207 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L185-L207) 的 `DFlashGeometry` 与主模型 `Config` 字段几乎同构。
- [src/config.rs:228-273](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L228-L273) — `RawDFlashConfig`：`#[serde(flatten)]` 把几何摊平进来，其余字段全部 `Option` 或带默认值（`markov_rank` 默认 0、`selector_top_k` 默认 `DFLASH2_CANDIDATE_K`、`layer_types` 缺省 `None`……）。
- [src/config.rs:521-524](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L521-L524) — 字段级联的标准写法：`inner.and_then(|c| c.block_size).or(raw.block_size).context("drafter config missing block_size")`——嵌套优先、顶层兜底、两处都没有就带着字段名报错。
- [src/config.rs:565-569](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L565-L569) — `rope_theta` 的双形态：平铺字段优先，其次 `rope_parameters.rope_theta`。
- [src/config.rs:571-595](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L571-L595) — `target_layer_ids` 的两来源与 `checked_sub(1)` 换算：Speculators 的 `aux_hidden_state_layer_ids` 以 HF `hidden_states` 编号（0 是 embedding），本 crate 的捕获点是 0-based 解码器输出，故逐项减一；减不了（0）说明引用了 embedding，直接报错。
- [src/config.rs:626-650](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L626-L650) — **layer_types 缺省补全**：显式给出时必须长度等于层数且全为 `full_attention`；没给出时模拟 transformers `Qwen3Config` 的推导——注释明言 "Mirrors how transformers' `Qwen3Config` derives absent layer types"，缺省窗口取 [176-181 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L176-L181) 的 `DEFAULT_SLIDING_WINDOW = 4096` 与 `default_max_window_layers() = 28`；若 `use_sliding_window` 开着、窗口存在、且 `max_window_layers < 层数`，则下方必有 sliding 层，DSpine 拒绝。
- [src/config.rs:674-678](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L674-L678) — `anchor_first` 归一化：`num_anchors.is_some() || markov_rank > 0`。[79-87 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L79-L87) 的注释解释了为什么这是 **checkpoint 的属性**而非 markov 头的属性——按 markov 头去推断会让无 markov 的 DeepSpec checkpoint 被错误路由，接受率塌到 ~0。
- [src/config.rs:712-793](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L712-L793) — `DFlashConfig::validate_for_target`：与主模型 `Config` 的跨配置校验——hidden/vocab/注意力几何必须相等、`rope_theta` 用 `to_bits()` 位级比较（[738-743 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L738-L743)）、`max_position_embeddings` 不得小于目标、`block_size >= 2`、`mask_token_id < vocab`、`target_layer_ids` 严格递增且落在目标层数内。
- [src/config.rs:281-298](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L281-L298) — `Eagle3Config`：直接 `derive(Deserialize)` 的对照组，多出的 `draft_vocab_size` 是缩减草稿词表（`d2t`/`t2d` 重映射，u6-l5 详讲）。
- [src/config.rs:803-847](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L803-L847) — `Eagle3Config::validate_for_target`：单层 midlayer（`num_hidden_layers == 1`）、`draft_vocab_size ∈ (0, vocab]`、同样的位级 `rope_theta` 比较。
- 入口与拒绝时机：[src/executor.rs:1297](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1297) — `DFlashConfig::from_file(path)?.ensure_servable()`：DSpine 桥的 checkpoint 在**目标模型加载之前**就被拒绝，让它死于自己的理由而不是后续 KV 定尺寸。对应的集成测试在 [src/config.rs:1313-1335](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L1313-L1335)。

已有测试展示了 Raw 层的「双形态等价」契约：

- [src/config.rs:917-946](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L917-L946) — 同一份 selector 字段分别写成 flat 与 nested 两种 JSON，解析出的 `DFlashConfig` 关键字段必须一致。
- [src/config.rs:948-971](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L948-L971) — Speculators 形态：`aux_hidden_state_layer_ids: [1, 3]` 断言换算为 `target_layer_ids == [0, 2]`。

#### 4.4.4 代码实践

1. **实践目标**：用现成测试验证「三种 schema → 一个 Config」的归一化承诺，并亲手构造一次 `checked_sub(1)` 边界拒绝。
2. **操作步骤**：
   - 运行既有归一化测试，阅读其断言：

     ```bash
     cargo test --release -p pegainfer-qwen3 --lib dflash_selector_fields_parse \
       && cargo test --release -p pegainfer-qwen3 --lib speculators_dflash_maps
     ```

   - 在 `tests` 模块补一个边界拒绝测试（示例代码）：把 [948-971 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L948-L971) 测试中的 `"aux_hidden_state_layer_ids": [1, 3]` 改成 `[0, 3]`（0 号是 embedding，不是解码器输出），断言 `DFlashConfig::from_file` 返回 `Err` 且信息含 `"must refer to a decoder output"`（文案见 [583-586 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L583-L586)）。
3. **需要观察的现象**：既有两测试通过；新测试证明 0 号索引被显式拒绝而不是悄悄变成 `usize::MAX`（`checked_sub` 的意义）。
4. **预期结果**：按源码推导应当全部通过；运行输出**待本地验证**。

#### 4.4.5 小练习与答案

1. **flat 与 nested 两份 JSON 都写了 `block_size`，谁赢？**
   答：nested 的 `dflash_config.block_size` 赢——级联写法是 `inner.block_size.or(raw.block_size)`（[521-524 行](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L521-L524)），inner 在前。
2. **`Eagle3Config` 为什么不需要 `Raw*` 层？**
   答：它的 checkpoint（如 `AngelSlim/Qwen3_eagle3`）只有一种 schema，字段与最终结构一一对应，直接 `derive(Deserialize)` 即可；Raw 层的存在只是为了吸收多种历史布局。
3. **`validate_for_target` 里比较 `rope_theta` 为什么用 `to_bits()` 而不是 `==`？**
   答：位级比较要求两个 f32 完全同比特（含符号/零/NaN 等特殊值的表示），是「配置必须逐位一致」的最强表述；普通 `==` 对 `-0.0 == 0.0` 这类情形会放行。RoPE 频率基不一致会让草稿与目标的位置编码错位，所以这里宁严勿松。

## 5. 综合实践

**任务：给三份伪造的 `config.json` 做一次「配置体检」，画出校验阶梯的拦截点。**

在一个临时测试（或独立 examples 文件）里准备三个模型目录：

| 目录 | 内容 | 预期被哪一阶拦截 |
|------|------|------|
| A | `{"model_type": "llama", ...}`（几何字段齐全但身份不对） | `probe_config_json`：model_type 不是 qwen3 |
| B | `{"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"], ...}` 但 `num_attention_heads: 31, num_key_value_heads: 8` | `Config::from_file` 的 GQA `ensure!`（serde 只查字段存在性，31/8 的倍数关系只有语义层能查） |
| C | 合法 8B 几何，但用 `TensorParallelConfig { rank: 0, world_size: 3 }` 去装载 | `validate_for` 第③条：32 头不可被 3 整除 |

步骤：

1. 对每个目录依次调用 `crate::probe_config_json(&json)` → `Config::from_file(dir)` → `TensorParallelConfig{0,3}.validate_for(&config)`，把每一步的 `Ok/Err` 与错误文案记录成表。
2. 回答：B 目录为什么能穿过 serde、死在 `ensure!`？C 目录为什么连 `from_file` 都拦不住、必须等到 `validate_for`？（提示：前者是**字段间关系**，后者是**字段与运行上下文（几张卡）的关系**——from_file 阶段根本不知道 world_size。）
3. 延伸：把 C 的 world_size 改成 4（32/8/12288 对 4 全部可整除），确认校验放行——并对照 u1-l3 里 `--tp-size` 属于共享 flag 的结论，理解「CLI 校验查标志组合、装载校验查几何可分性」的分工。
4. 运行方式：写成 `#[test]`（三个目录用 `tempfile::tempdir` 构造），`cargo test --release -p pegainfer-qwen3 --lib config_ladder` 运行；具体输出**待本地验证**。

这个实践把本讲的四个模块串成一条线：身份（probe）→ schema（serde）→ 语义（from_file）→ 上下文（TP 校验）→（延伸）跨配置（`validate_for_target`）。

## 6. 本讲小结

- **分层校验阶梯**：`probe_config_json` 只做身份检查（model_type + architectures）；serde 派生负责 schema（缺字段/错类型）；`from_file` 负责字段间语义（GQA 倍数、eos 读取）；`TensorParallelConfig::validate_for` 负责与运行上下文的关系（可除性）；草稿器另有 `validate_for_target` 做跨配置一致性。每层只做自己那层能做的检查，失败得越早越便宜。
- **`Config` 是三层字段的混合体**：JSON 同名字段 + serde 默认值（`max_position_embeddings` 缺省 40960）+ `#[serde(skip)]` 程序填充（`decodable_vocab`/`logits_rows`/`stop_token_ids`）；不 `deny_unknown_fields` 是对 HF 生态杂项字段的必要宽容。
- **硬失败与软降级并存**：GQA 非正倍数直接 `Err`；比值不在内核支持表 `[1,2,3,4,6,8]` 只 `warn`（eager 解码、CUDA 图解码关闭、`--decode-overlap` 不可用）。
- **TP 切层内宽度，不切层**：`validate_for` 校验头数/KV 头数/中间维的可除性，`num_hidden_layers` 不参与；`shard_range` 与 `local_*` 家族是纯函数推导；GQA 比值 TP 不变。
- **多 schema 用 Raw 层归一化**：`RawDFlashConfig`（配合 untagged 的 `DFlashGeometrySource`）把 flat / nested / Speculators 三种布局吸收成一个 `DFlashConfig`，字段级联「嵌套优先、顶层兜底」；`Eagle3Config` 单 schema 免 Raw 层；`layer_types` 缺省补全只存在于草稿器一侧（镜像 transformers 的推导，窗口缺省 4096、max_window_layers 缺省 28）。

## 7. 下一步学习建议

配置解析只是静态侧的第一步。建议：

1. **下一讲 u2-l2（权重布局）**：看 `Config` 的这些字段如何决定 `AttentionWeights`/`MlpWeights` 的矩阵形状与 QKV 合存布局——本讲的 `local_q_dim`/`local_kv_dim` 会直接变成权重矩阵的行数。
2. **再往后的 u2-l3（checkpoint 装载）**：跟踪 `Config::from_file` 之后的 `load_shard_info` 与两阶段装载，以及「先记录计划再上传」如何用本讲的 `shard_range` 切 TP 行。
3. **随手验证**：打开一份真实的 `models/Qwen3-4B/config.json`，对照 [src/config.rs:27-49](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L27-L49) 逐字段确认哪些会被读入、哪些被忽略；再数一数 JSON 里有而 `Config` 没有的字段（如 `architectures`、`torch_dtype`），体会「宽容吸收」的边界。
