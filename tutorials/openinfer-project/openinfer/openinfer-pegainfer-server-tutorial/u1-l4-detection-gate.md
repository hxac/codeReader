# u1-l4 · 检测门：config.json 判定与 feature 提示

## 1. 本讲目标

上一讲（u1-l3）我们把 `main()` 走了一遍，其中「阶段 6：detect 唯一认领模型线」只是一行代码带过。本讲把这扇门单独拆开，学完后你应当能够：

1. **解释 `registry.detect` 的三种结果**：唯一认领（成功）、`Conflict`（两条线同时认领）、`NoMatch`（无人认领，且错误里带着每条编译线的拒绝原因）。
2. **说明 `families()` 为什么是「必要的第二真相源」**：被编译掉的模型线连 `probe` 函数都不存在于二进制里，提示所需的身份字符串只能硬编码在 server 这张表里。
3. **读懂 `tests/detect_gate.rs` 集成测试**：它如何用一份冻结 config、一个最小 tokenizer 驱动**真实二进制**，分别断言 stock 构建（提示 `--features qwen4`）与 enabled 构建（认领后拒绝服务）两种出厂行为——全程不触达 CUDA。

本讲的实践全部可以在**没有 GPU** 的机器上完成，因为检测门立在引擎加载之前。

## 2. 前置知识

- **`config.json` 与 `model_type`**：Hugging Face 格式的模型目录里，`config.json` 是模型的「身份证」。其中 `model_type`（如 `"qwen3"`、`"glm_moe_dsa"`）标识架构家族，`architectures`（如 `["Qwen3ForCausalLM"]`）列出具体顶层结构。多模态模型还会把文本塔配置嵌在 `text_config` 对象里，其中有自己的 `model_type`。
- **cargo feature 与条件编译**（u1-l2 已讲）：`pegainfer-server` 的 `default = ["qwen3"]`，其他模型线要靠 `--features` 拉入。被拉入与否是**编译期**事实——没被拉入的 crate 的代码根本不在二进制里。`cfg!(feature = "xxx")` 是一个在编译期求值为 `true`/`false` 的表达式。
- **`probe`（探针）**：每条模型线自带的一个函数，输入解析后的 `config.json`，输出「认领」或「拒绝 + 原因」。它是检测门的判断核心。
- **`thiserror` 错误模板**：Rust 里用派生宏把枚举变体渲染成人类可读的错误字符串，本讲会看到两段模板的逐字段拼装。
- **集成测试小工具**：Cargo 会把 `tests/` 目录下的文件编译成独立测试二进制；`env!("CARGO_BIN_EXE_pegainfer")` 在编译期展开为「被测包的二进制路径」；`include_str!` 在编译期把文件内容嵌成字符串常量。三者合起来就能「跑真二进制、断言其 stderr」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs) | server 二进制：`Family` 身份表、`families()`、`feature_gate_hint()`、`main()` 里的接线、4 个 hint 单测 |
| [tests/detect_gate.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs) | 集成测试：构造临时模型目录，驱动真实二进制，断言 stock / enabled 两种出厂行为 |
| [../pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs) | `ModelLine` trait（`probe` 契约）、`DetectError` 枚举、`ModelLineRegistry::detect` 的判定循环 |
| [../pegainfer-qwen3/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs) | 一个真实 `probe` 实现（先查 `model_type` 再委托身份检查）及其单测 |
| [../pegainfer-qwen3/src/config.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/config.rs) | `probe_config_json`：Qwen3 的身份检查（`model_type` + `architectures`） |
| [../pegainfer-qwen4/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen4/src/model_line.rs) | 检测成功但 `launch` 明确拒绝的线（enabled 测试的断言目标） |
| [../pegainfer-qwen4/tests/frozen_config.json](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen4/tests/frozen_config.json) | 冻结的真实 checkpoint config（集成测试的 fixture 之一） |
| [../pegainfer-frontend/examples/echo-model/tokenizer.json](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/examples/echo-model/tokenizer.json) | 仓库自带的最小 BPE tokenizer（fixture 之二） |

## 4. 核心概念与源码讲解

### 4.1 检测门的判定：`registry.detect` 与三种结果

#### 4.1.1 概念说明

「检测门」是 `main()` 在读完 `config.json` 之后、做任何昂贵动作（引擎加载）之前的一步裁决：**这份配置到底归哪条编译进来的模型线管？**

设计约束写在 `probe` 的契约里：**恰好一条线必须接受一份给定配置**。由此产生三种结果：

1. **唯一认领**：恰好一条线 `probe` 返回 `Ok` → 检测通过，返回这条线的 `&'static dyn ModelLine`，`main()` 继续往下走。
2. **`Conflict`**：两条线都返回 `Ok` → 这是**注册表层面的 bug**（两条线的身份表重叠了），必须立刻失败并点名双方。
3. **`NoMatch`**：没有任何线认领 → 用户拿错了模型或二进制。错误信息不只是一句「不认识」，而是**逐条列出每条编译线拒绝的原因**，让用户能自己看出差在哪。

`NoMatch` 还有一个特殊待遇：`main()` 会在它后面追加一条「feature 提示」（4.3 节）。

#### 4.1.2 核心流程

`detect` 的判定循环可以写成这样的伪代码：

```text
rejections = []
claimed = None
for line in 编译进来的每条线（按注册顺序）:
    match line.probe(config):
        Ok  => if claimed 为空: claimed = line
               else:           立即返回 Conflict { first: 已占者, second: 后来者 }
        Err(reason) => rejections.push("{line.name}: {reason}")
claimed 存在 => 返回 claimed
否则 => 返回 NoMatch {
    model_type:     config.model_type（缺失则 "missing"）
    architectures:  config.architectures（缺失则 "missing"）
    rejections:     rejections 用 "; " 连接
}
```

两个值得注意的细节：

- `Conflict` **立刻返回**，不再继续探测——错误已经足够定性；
- `NoMatch` 则**收集完所有拒绝原因**才返回——信息量最大化，用户能看到每条线的说法。

#### 4.1.3 源码精读

先看错误类型。两种失败各自的模板（[../pegainfer-frontend/src/model_line.rs:L24-L46](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L24-L46)）：

```rust
pub enum DetectError {
    #[error("model config claimed by both {first} and {second}")]
    Conflict { first: &'static str, second: &'static str },
    #[error(
        "no compiled-in model line claims this config (model_type={model_type}, architectures={architectures}); rejections: {}",
        rejections.join("; ")
    )]
    NoMatch { model_type: String, architectures: String, rejections: Vec<String> },
}
```

这段代码定义了检测失败的两类错误：`Conflict` 点名两条抢配置的线；`NoMatch` 渲染身份字段并拼接全部拒绝原因。其上方文档注释（L24-L26）明说了分工：**server 只在 `NoMatch` 上分支去追加 feature 提示，其余直接渲染**——这正是本讲 4.3 的接线点。

再看判定循环本体（[../pegainfer-frontend/src/model_line.rs:L569-L601](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L569-L601)）：遍历 `self.entries`、首占与冲突、`rejections.push(format!("{}: {reason}", entry.line.name()))`、最后 `claimed.ok_or_else(...)` 组装 `NoMatch`。其中 `render` 闭包对缺失字段填 `"missing"`，对存在字段调 `to_string()`（所以 `architectures` 会以 JSON 文本原样出现，例如 `["Qwen3ForCausalLM"]`）。

`probe` 的契约定义在 trait 上（[../pegainfer-frontend/src/model_line.rs:L341-L344](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L341-L344)）：

```rust
/// Claim or reject a model directory by its parsed `config.json`.
/// Return `Err` with the reason when the architecture doesn't match;
/// exactly one registered line must accept a given config.
fn probe(&self, config: &serde_json::Value) -> Result<(), String>;
```

一个真实实现（Qwen3 线，[../pegainfer-qwen3/src/model_line.rs:L137-L143](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L137-L143)）：先做一次快速的 `model_type` 预筛，再委托 `probe_config_json` 做完整身份检查。后者（[../pegainfer-qwen3/src/config.rs:L894-L910](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/config.rs#L894-L910)）只查两件事：`model_type == "qwen3"`、`architectures` 包含 `"Qwen3ForCausalLM"`——注释明说这是 **identity check**（身份检查），尺寸与形状校验属于配置加载器。检测门只问「你是谁」，不问「你多高」。

frontend 里还有一组现成单测覆盖三种结果（可作交叉参照）：`detect_finds_the_unique_claimant`、`detect_conflict_names_both_lines`、`detect_no_match_renders_identity_fields_verbatim`、`detect_no_match_reports_missing_fields`（[../pegainfer-frontend/src/model_line.rs:L845-L884](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L845-L884)）。

#### 4.1.4 代码实践

**目标**：亲眼看到 `NoMatch` 错误里「rejections 逐条列出拒绝原因」的构造，并验证「已编译的线不触发提示」。

1. 建一个目录，写一份**身份半对**的 config——`model_type` 是 qwen3，但 `architectures` 是假的：

   ```bash
   mkdir -p /tmp/detect-lab/badarch
   cat > /tmp/detect-lab/badarch/config.json <<'EOF'
   {"model_type": "qwen3", "architectures": ["SomethingElse"]}
   EOF
   ```

2. 用 stock 构建（只编了 qwen3 线）运行：

   ```bash
   cargo run --release -p pegainfer-server -- --model-path /tmp/detect-lab/badarch
   ```

3. **观察现象**：进程应当立刻以非零码退出，stderr 里是一行 `Error:` 开头的 `NoMatch` 错误。

4. **预期结果**（由源码逐字段推导；精确排版待本地验证）：

   ```text
   Error: no compiled-in model line claims this config (model_type=qwen3, architectures=["SomethingElse"]); rejections: Qwen3: Qwen3 architectures must contain Qwen3ForCausalLM
   ```

   逐段对账：`model_type=qwen3` 与 `architectures=["SomethingElse"]` 来自 `render` 闭包；`Qwen3: Qwen3 architectures must contain Qwen3ForCausalLM` 是 Qwen3 线的 `probe` 先过 `model_type` 预筛（[../pegainfer-qwen3/src/model_line.rs:L137-L141](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L137-L141)）、再在 [../pegainfer-qwen3/src/config.rs:L905-L908](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/config.rs#L905-L908) 的 `ensure!` 处失败给出的原因；错误里**没有** `--features` 提示——qwen3 是已编译的线，提示机制（4.3 节）只服务「存在但被编译掉」的家族。这与单测 `probe_rejects_bad_architectures`（[../pegainfer-qwen3/src/model_line.rs:L436-L439](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L436-L439)）断言的是同一个行为。

#### 4.1.5 小练习与答案

**练习 1**：`Conflict` 什么时候出现？它是用户错误还是代码 bug？

**答案**：两条注册线的 `probe` 对同一份 config 都返回 `Ok` 时出现（[../pegainfer-frontend/src/model_line.rs:L577-L584](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L577-L584)）。用户无法通过换模型目录触发它——它说明两条模型线的身份表（各自认领的 `model_type` 集合）发生了重叠，属于注册表层面的 bug。以错误而非 panic 呈现，是因为它毕竟要在运行期被发现，且错误信息（点名双方）已足够定位。

**练习 2**：为什么 `Conflict` 立即 `return`，而 `NoMatch` 要收集**全部** rejections 才返回？

**答案**：`Conflict` 的错误语义已经完备——两条线名字就是全部事实，继续探测没有增量信息。`NoMatch` 则相反：用户拿到的信息越多越能自己定位（「哦，原来所有线都嫌我的 `model_type` 不对」），所以逐条收集每条线的拒绝原因拼进错误。

**练习 3**：为什么 Qwen3 的 `probe` 只查 `model_type` 和 `architectures`，不检查隐藏层尺寸、词表大小这些字段？

**答案**：`probe_config_json` 的文档注释（[../pegainfer-qwen3/src/config.rs:L894](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/config.rs#L894)）写明这是 **identity check**：「尺寸与形状校验属于配置加载器」。检测门要回答的只是「这条线认不认领此目录」；形状问题留给后续的 `line.validate`/`launch` 阶段，那时才值得做更贵的解析。

### 4.2 `families()` 身份表：必要的第二真相源

#### 4.2.1 概念说明

4.1 节的 `detect` 只能调用**编译进来的**线的 `probe`。于是出现一个盲区：用户拿着一份 GLM5.2 的配置跑 stock 二进制（只编了 qwen3），`detect` 只会得到 `NoMatch`——**没有任何代码能告诉用户「其实项目支持这条线，你只是没编进去」**，因为 glm52 的 crate 根本不在这个二进制里，它的 `probe`、它的 `model_type` 字符串都不存在。

解决办法是在 server 里维护一张**身份表** `families()`：每一行记录「feature 名 + 这条线的 `probe` 会认领哪些 `model_type`（顶层与 `text_config` 各一组）+ 这条线在本二进制里是否被编译」。文档注释给它定性：**a second source of truth by necessity**（必要的第二真相源）——「提示存在的意义恰恰是那条线（连同它的 `probe`）被编译掉的场合，所以这些字符串不可能从线 crate 里读出来」，只能硬编码在此。

`compiled` 字段用 `cfg!(feature = "...")` 求值：这是**编译期常量**，随本二进制的 feature 集合而定——同一份 `main.rs`，stock 构建里 glm52 行的 `compiled` 是 `false`，`--features glm52` 构建里是 `true`。

#### 4.2.2 核心流程

表的每一行是一个四字段结构：

```text
Family {
    feature:          cargo feature 名（也是提示里 --features 的参数）
    model_types:      该线 probe 认领的顶层 model_type 列表
    text_model_types: 该线 probe 认领的 text_config.model_type 列表
    compiled:         cfg!(feature = ...) —— 本二进制是否编入该线
}
```

表在 `families()` 里按字母序列出全部 8 条线，返回定长数组 `[Family; 8]`。它的生命周期与维护纪律：

- **新增模型线**必须同步加一行，否则「被编译掉的提示」对该线永远沉默（这是 u5-l2 接入清单的一项）；
- 返回类型是**定长数组**，行数写死在签名里——多写一行少写一行都会直接类型不匹配，但「整行忘了加」编译器管不着，只能靠测试兜底；
- 兜底测试的范例是 qwen4 行：`qwen4_hint_identity_matches_the_line_crate` 把表里的字符串与线 crate 自己的 `MODEL_TYPE`/`TEXT_MODEL_TYPE` 常量做相等断言，改了一边不改另一边就失败。

#### 4.2.3 源码精读

`Family` 结构（[src/main.rs:L45-L52](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L45-L52)）与 `families()` 的文档注释（[src/main.rs:L54-L66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L54-L66)）是本模块最值得逐句读的文字。注释里有两处关键限定：

1. **「by necessity」**——这些字符串不能从线 crate 读（线被编译掉了），所以硬编码是唯一选择，qwen4 行靠 feature-gated 测试绑住两端；
2. **「identity-only」**——提示无法运行被编译掉的 `probe`，所以即使某个**量化变体**（`quantization_config` 不同）、绑词嵌入（tied embeddings）或 YaRN rope 的同家族模型会被真 `probe` 拒绝，提示照样给出。它只承诺「重建用的 feature 名」，**不承诺这个 checkpoint 可服务**。

表本体（[src/main.rs:L67-L118](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L67-L118)）节选三行感受结构：

```rust
Family {
    feature: "glm52",
    model_types: &["glm_moe_dsa"],
    text_model_types: &[],
    compiled: cfg!(feature = "glm52"),
},
...
Family {
    feature: "kimi-k2",
    model_types: &["kimi_k25", "kimi_k2"],
    text_model_types: &["kimi_k2"],
    compiled: cfg!(feature = "kimi-k2"),
},
...
Family {
    feature: "qwen4",
    model_types: &["qwen4_exp"],
    text_model_types: &["qwen4_exp_text"],
    compiled: cfg!(feature = "qwen4"),
},
```

注意两个容易忽略的点：`model_types` 可以有多个成员（kimi-k2 认领 `kimi_k25` 与 `kimi_k2` 两种拼写）；多模态线在 `text_model_types` 里放了**文本塔**的身份（gemma4 的 `gemma4_unified_text`、qwen4 的 `qwen4_exp_text`），让嵌套在 `text_config` 里的身份也能命中提示。

#### 4.2.4 代码实践

**目标**：核对身份表与 feature 矩阵的一一对应，并亲手验证 `compiled` 是编译期取值。

1. 打开 [Cargo.toml:L34-L48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L34-L48)，数一数 `[features]` 里**模型线 feature** 的个数（`qwen3`、`deepseek-v2-lite`、`gemma4`、`glm52`、`k3`、`kimi-k2`、`qwen35`、`qwen4`——`default` 只是默认集，不算线）。
2. 对照 `families()` 的 8 行（[src/main.rs:L67-L118](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L67-L118)），确认 feature 名一一对得上。
3. 阅读绑定测试 [src/main.rs:L324-L337](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L324-L337)：它整个被 `#[cfg(feature = "qwen4")]` 守卫——只有当 qwen4 线**被编译进来**、它的 `MODEL_TYPE` 常量可被 import 时，这个「表与 crate 不得漂移」的断言才会编译并运行。
4. **观察/预期**：这是一次纯阅读核对，产出一张「feature → 认领的 model_type → 认领的 text_model_type」三列表；预期 8 行全部能对上（u1-l1 已指出：README 只列 7 条线，qwen4 是未写入文档的实验线，但在表和 Cargo.toml 里都有）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `families()` 的字符串不直接从各模型 crate 读常量，非要硬编码？

**答案**：因为提示的**服务对象**恰恰是被编译掉的线。stock 二进制里没有链接 glm52 crate，`pegainfer_glm52::model_line` 这个模块在编译期就不存在，任何 `use` 都无法通过编译。身份表必须在「没有任何线 crate 代码」的前提下仍然知道全部家族的身份——这就是「必要的第二真相源」的含义（[src/main.rs:L57-L61](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L57-L61)）。

**练习 2**：用户拿着一份 `model_type=qwen3` 但带 `quantization_config` 的量化模型跑 stock 构建，`detect` 失败后会得到什么？这个输出在什么意义上「过度承诺」？

**答案**：qwen3 是已编译家族，`feature_gate_hint` 对它保持沉默（`compiled` 为 `true` 直接跳过），用户只会看到 `NoMatch` 与 Qwen3 线 `probe` 的具体拒绝原因——这是准确的信息。反过来，若量化的是 glm52 家族，用户会看到 `--features glm52` 提示；但提示是 **identity-only**（[src/main.rs:L63-L66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L63-L66)）：它没运行过真 `probe`，无法知道该变体的量化配置会被拒绝。「过度承诺」在于：它保证的只是「重建后 detect 会看到这个家族」，不是「重建后能服务这个 checkpoint」。

**练习 3**：`families()` 返回 `[Family; 8]` 定长数组。这个设计能防住哪类错误、防不住哪类错误？

**答案**：能防**行数与声明不符**——比如加了一行忘了把签名改成 `[Family; 9]`，类型不匹配直接编译失败。防不住**整行遗漏**——8 行漏写 1 行仍是合法的 `[Family; 8]`，编译照过，只是那条被编译掉的线永远得不到提示。所以需要 qwen4 绑定测试这类「表 ↔ 线 crate 常量」的运行期对账来兜底。

### 4.3 `feature_gate_hint()`：把身份翻译成 rebuild 建议

#### 4.3.1 概念说明

有了身份表，剩下的就是把「这份 config 属于某个**未编译**家族」翻译成一句可操作的建议。`feature_gate_hint(config)` 做的就是这件事：从 config 里取两个身份键（顶层 `model_type` 与 `text_config.model_type`），在 `families()` 里找**第一个**「未编译且身份匹配」的行，命中则返回一句固定格式的提示：

```text
this looks like a {feature} model; rebuild pegainfer-server with --features {feature}
```

它在 `main()` 里的接线位置（u1-l3 的阶段 6 内部）：`detect` 失败且**恰好是 `NoMatch`** 时，才把提示用 `"; "` 拼在原始错误后面一起返回；`Conflict` 或成功路径完全不经此分支。也就是说，最终用户看到的 `NoMatch` 错误是「判定细节 + 行动建议」两段式。

#### 4.3.2 核心流程

```text
feature_gate_hint(config):
    model_type      = config.model_type        （缺失则 ""）
    text_model_type = config.text_config.model_type（缺失则 ""）
    在 families() 里找第一行满足：
        !compiled
        且 (model_types 含 model_type 或 text_model_types 含 text_model_type)
    命中 => 返回 "this looks like a {feature} model; rebuild ... with --features {feature}"
    未命中 => 返回 None（未知家族 → 无提示）
```

`main()` 侧的接线：

```text
registry.detect(config) 失败 =>
    if 是 NoMatch 且 feature_gate_hint(config) 命中:
        返回错误 "{NoMatch 错误}; {hint}"
    else:
        原样返回错误
```

#### 4.3.3 源码精读

提示函数本体（[src/main.rs:L123-L144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L123-L144)）：

```rust
fn feature_gate_hint(config: &serde_json::Value) -> Option<String> {
    let model_type = config.get("model_type").and_then(serde_json::Value::as_str).unwrap_or("");
    let text_model_type = config.get("text_config")
        .and_then(|text| text.get("model_type"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    families().iter().find_map(|family| {
        (!family.compiled
            && (family.model_types.contains(&model_type)
                || family.text_model_types.contains(&text_model_type)))
        .then(|| format!(
            "this looks like a {} model; rebuild pegainfer-server with --features {}",
            family.feature, family.feature
        ))
    })
}
```

这段代码先取两个身份键，再用 `find_map` 在身份表里找第一个未编译且身份匹配的行并格式化提示。注意 `find_map` 按 `families()` 的表序短路——理论上若多个未编译家族共享同一 `model_type`，只有表里靠前者会被提示（当前表无此重叠）。

`main()` 的接线（[src/main.rs:L168-L175](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L168-L175)）：

```rust
let line = registry.detect(&config).map_err(|error| {
    if matches!(error, DetectError::NoMatch { .. }) {
        if let Some(hint) = feature_gate_hint(&config) {
            return anyhow::anyhow!("{error}; {hint}");
        }
    }
    anyhow::Error::from(error)
})?;
```

这段代码把提示的适用范围收窄到 `NoMatch` 一种情形：`Conflict` 是注册表 bug，与用户该重建什么无关；而 `NoMatch` 正是「可能拿了未编译家族的模型」的信号。提示以 `"; "` 拼接在判定错误之后，用户一次看到「为什么失败」与「怎么办」。

单测四连覆盖了提示的肯定与否定两面（[src/main.rs:L291-L337](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L291-L337)）：

- `hint_names_the_feature_for_a_known_but_uncompiled_family`（L295-L301，`#[cfg(not(feature = "glm52"))]`）：`{"model_type": "glm_moe_dsa"}` → 提示含 `--features glm52`；
- `hint_matches_text_config_identities`（L303-L309）：`{"text_config": {"model_type": "gemma4_unified_text"}}` → 命中 `text_model_types`，提示 `--features gemma4`；
- `hint_is_silent_for_a_compiled_family`（L311-L316）：`model_type=qwen3` 在 stock 构建下无提示；
- `hint_is_silent_for_an_unknown_family`（L318-L322）：`model_type=frobnicate_lm` 无提示。

注意前两个测试自身带 `#[cfg(not(feature = ...))]` 守卫——在启用了对应 feature 的构建里，「未编译」这个前提不成立，测试自动退场。

#### 4.3.4 代码实践

**目标**：亲手触发一次「已知但未编译家族」的提示，再验证未知家族不触发。这是本讲的主实践，全程无 GPU（失败发生在引擎加载之前）。

1. 构造模型目录：写一份 GLM5.2 身份的最小 config，并放一个 tokenizer（复制仓库自带的最小 tokenizer，让目录形态完整；本次失败发生在 detect，tokenizer 并不被读到——它何时不可缺见 4.4 节）：

   ```bash
   mkdir -p /tmp/detect-lab/glm52
   cp ../pegainfer-frontend/examples/echo-model/tokenizer.json /tmp/detect-lab/glm52/
   cat > /tmp/detect-lab/glm52/config.json <<'EOF'
   {"model_type": "glm_moe_dsa"}
   EOF
   ```

   （若在仓库根目录执行，把 `cp` 的源路径相应调整；关键是复制 `pegainfer-frontend/examples/echo-model/tokenizer.json` 这个文件。）

2. 用 stock 构建运行：

   ```bash
   cargo run --release -p pegainfer-server -- --model-path /tmp/detect-lab/glm52
   ```

3. **观察现象**：立即非零退出，stderr 一行 `Error:`。

4. **预期结果**（由源码逐字段推导；精确排版待本地验证）：

   ```text
   Error: no compiled-in model line claims this config (model_type=glm_moe_dsa, architectures=missing); rejections: Qwen3: model_type Some("glm_moe_dsa") is not "qwen3"; this looks like a glm52 model; rebuild pegainfer-server with --features glm52
   ```

   对账：前半段是 `NoMatch` 模板（`architectures` 缺失渲染为 `missing`；唯一编译线 Qwen3 在 `model_type` 预筛处拒绝，原因文本来自 [../pegainfer-qwen3/src/model_line.rs:L139-L141](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L139-L141)）；`"; "` 之后就是 `feature_gate_hint` 的输出，glm52 行命中（`model_types` 含 `glm_moe_dsa` 且未编译）。

5. 换成未知家族再跑一次：

   ```bash
   mkdir -p /tmp/detect-lab/unknown
   cp /tmp/detect-lab/glm52/tokenizer.json /tmp/detect-lab/unknown/
   echo '{"model_type": "frobnicate_lm"}' > /tmp/detect-lab/unknown/config.json
   cargo run --release -p pegainfer-server -- --model-path /tmp/detect-lab/unknown
   ```

   **预期**：仍是 `NoMatch`（rejections 同样只有 Qwen3 一条），但**没有任何 `--features` 提示**——`frobnicate_lm` 不在任何家族的身份表里，`find_map` 落空返回 `None`，与单测 `hint_is_silent_for_an_unknown_family` 断言的行为一致。

6. 顺手跑一遍 4 个 hint 单测（stock 构建下应见 4 个通过）：

   ```bash
   cargo test --release -p pegainfer-server --bin pegainfer hint
   ```

#### 4.3.5 小练习与答案

**练习 1**：为什么提示要**拼在** `NoMatch` 错误后面，而不是单独输出一句「请用 --features glm52 重建」？

**答案**：拼接（`"{error}; {hint}"`）保证了用户同时拿到**判定证据**与**行动建议**：rejections 里的 `Qwen3: model_type ... is not "qwen3"` 说明了「谁看了、为什么不要」，提示说明「下一步做什么」。只给建议不给证据，用户无法区分「模型目录放错了」与「真的需要重建」两种情形；只给证据不给建议，用户要自己去翻文档找 feature 名。

**练习 2**：`text_model_types` 这个查找键是为了什么场景存在的？

**答案**：多模态模型的 config 常把文本塔参数嵌在 `text_config` 对象里，其 `model_type`（如 `gemma4_unified_text`、`qwen4_exp_text`）可能与顶层不同。只查顶层 `model_type` 会漏掉这类身份，所以身份表为每行多维护一组 `text_model_types`，`feature_gate_hint` 同时取 `text_config.model_type` 参与匹配（[src/main.rs:L128-L136](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L128-L136)）。单测 `hint_matches_text_config_identities` 专门断言这条通路。

**练习 3**：stock 构建下用一份合法的 Qwen3-4B 配置运行，`feature_gate_hint` 会命中 qwen3 行吗？

**答案**：不会。qwen3 行的 `compiled` 在 stock 构建下是 `true`，而匹配条件里有 `!family.compiled` 这一环——提示只服务「存在但被编译掉」的家族。合法 Qwen3 配置会在 `detect` 处被唯一认领，根本走不到错误分支；即便 config 有 `architectures` 之类的身份错误（4.1.4 实践），得到的也只是纯 `NoMatch`，无提示。

### 4.4 `detect_gate` 集成测试：驱动真实二进制验证这道门

#### 4.4.1 概念说明

到 4.3 为止，检测门的每个部件（`probe`、`detect`、`families`、`feature_gate_hint`）都有自己的单测。但**部件正确 ≠ 接线正确**：`main()` 读 `config.json`、调 `registry.detect`、在 `NoMatch` 上拼提示、再到 `launch` 的拒绝——这条链路只有把二进制真的跑起来才会被执行。`tests/detect_gate.rs` 的模块文档（[tests/detect_gate.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L1-L11)）开头就交代了这层动机，并点明它断言的是**两种出厂行为**：

- **stock 构建**（未编 qwen4）：检测门对 `qwen4_exp` 配置给出「named feature」提示；
- **enabled 构建**（`--features qwen4`）：检测门放行，`launch` 明确拒绝服务（qwen4 尚未实现推理）。

文档最后一句是关键承诺：**两条路径都不触达 CUDA**——检测是纯计算，`launch` 在引擎构建之前就 bail 了。这让这个端到端测试可以跑在没有 GPU 的 CI 机器上。

选 qwen4 做探针是很讲究的：它是唯一一条「能通过 detect 但 launch 拒绝」的线，一个 fixture 就把门的**两侧**都测到了。

#### 4.4.2 核心流程

测试的组装流水线：

```text
两个编译期 fixture:
    FROZEN_CONFIG = include_str!(../pegainfer-qwen4/tests/frozen_config.json)  ← 真 checkpoint 的冻结 config
    TOKENIZER     = include_str!(../pegainfer-frontend/examples/echo-model/tokenizer.json) ← 仓库最小 tokenizer

model_dir(tag):
    在 $TMP/pegainfer-detect-gate-{tag}-{pid} 下写 config.json + tokenizer.json

run_against(dir):
    spawn $CARGO_BIN_EXE_pegainfer --model-path dir   ← 真二进制
    stderr 重定向到文件（防管道写满阻塞子进程）
    每 50ms try_wait 轮询；超过 60s 杀掉并 panic

两个用例（按构建 feature 二选一编译）:
    #[cfg(not(feature = "qwen4"))] stock_build_names_the_qwen4_feature
        断言: 退出非成功 且 stderr 含 "--features qwen4"
    #[cfg(feature = "qwen4")]     enabled_build_detects_the_line_and_refuses_to_serve
        断言: 退出非成功 且 stderr 含 "Qwen4" 且 含 "not implemented"
```

#### 4.4.3 源码精读

**Fixture 一：冻结的真 config**（[tests/detect_gate.rs:L22-L27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L22-L27)）。注释说明「server 自己不放 fixture，这道门就该看真东西」——这份文件由 qwen4 线 crate 提交，顶层 `model_type` 是 `"qwen4_exp"`（[../pegainfer-qwen4/tests/frozen_config.json:L7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen4/tests/frozen_config.json#L7)），文本塔身份 `"qwen4_exp_text"` 在 [../pegainfer-qwen4/tests/frozen_config.json:L85](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen4/tests/frozen_config.json#L85)——恰好对应身份表 qwen4 行的两个键。

**Fixture 二：最小 tokenizer 及其存在理由**（[tests/detect_gate.rs:L29-L37](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L29-L37)）。这段注释是全文件最值得一读的设计说明：enabled 构建里，`launch` 拒绝之前**HTTP 前端已经并发启动**（u1-l3 的双时间线），而前端没有 tokenizer 会拒绝启动——那样 stderr 里出现的就是 tokenizer 错误，把真正的 `launch` 拒绝**顶掉了**。放进 echo-model 的最小 BPE tokenizer（一份极小的词表文件，见 [../pegainfer-frontend/examples/echo-model/tokenizer.json:L1-L27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/examples/echo-model/tokenizer.json#L1-L27)），让前端正常起来、等引擎失败再把整个 server 拉倒，refusal 才是最终到达 stderr 的那个错误。

**运行器**（[tests/detect_gate.rs:L39-L83](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L39-L83)）三个工程细节：

- `env!("CARGO_BIN_EXE_pegainfer")` 编译期给出被测二进制路径——测试驱动的就是**出厂形态的那个 `pegainfer`**，不是库函数；
- stderr 写进**文件**再读回，注释写明「a full pipe cannot stall the child」：若用管道且测试侧不及时读，子进程把管道缓冲写满后会卡在 write 上永不退出；
- 50ms 一次 `try_wait` 轮询 + 60 秒上限（[tests/detect_gate.rs:L50-L52](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L50-L52)），注释说二进制暖机后一秒内就会退出，60 秒只是为了兜住「启动或取消路径永远不返回」的回归（比如引擎失败后 server 没被正确拉倒）。

**两个用例**（[tests/detect_gate.rs:L85-L119](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L85-L119)）：stock 用例断言非成功退出 + stderr 含 `--features qwen4`；enabled 用例断言非成功退出 + stderr 含 `Qwen4`（来自 `main` 的 `=== pegainfer - Qwen4 (GPU) ===` 启动横幅与 `failed to start Qwen4 engine` 上下文）且含 `not implemented`（来自 [../pegainfer-qwen4/src/model_line.rs:L29-L31](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen4/src/model_line.rs#L29-L31) 的 `launch`：`anyhow::bail!("this qwen4_exp checkpoint is detected, but inference is not implemented yet")`）。两个测试各自被互斥的 `#[cfg]` 守卫——同一份代码，构建决定谁上场。

#### 4.4.4 代码实践

**目标**：分别以 stock 与 enabled 两种构建跑这个集成测试，体会「同一份测试代码、两种出厂行为」，并说清两种断言各自验证什么。

1. stock 路径（无需 GPU；需可完成 CUDA 工具链编译——这是构建期依赖，不是运行期）：

   ```bash
   cargo test --release -p pegainfer-server --test detect_gate
   ```

2. enabled 路径：

   ```bash
   cargo test --release -p pegainfer-server --features qwen4 --test detect_gate
   ```

3. **观察现象**：两次都应看到 1 个测试通过（`stock_build_names_the_qwen4_feature` 或 `enabled_build_detects_the_line_and_refuses_to_serve`，取决于构建）。

4. **预期结果与断言解读**（精确测试名以本地输出为准，待本地验证）：

   - **stock 断言**验证的是「**未编译线 → NoMatch + feature 提示**」这条完整接线：真二进制读 `config.json` → `registry.detect` 无人认领 → `feature_gate_hint` 命中 qwen4 行 → `--features qwen4` 出现在最终错误里。任何一个环节（读文件、detect、提示拼接、错误传播到 stderr）断裂，这个测试都会失败。
   - **enabled 断言**验证的是门的**另一侧**：「**已编译线 → detect 认领 → 走到 launch → 诚实拒绝**」。`stderr` 含 `Qwen4` 证明 detect 成功认领并进入了该线的启动横幅；含 `not implemented` 证明失败来自 `launch` 的明确 bail（而不是 tokenizer 缺失、CUDA 初始化之类的**别的**错误）。这正是 TOKENIZER fixture 存在的意义。

5. 若想亲眼看两种 stderr：把 4.3.4 实践里的 `config.json` 换成 `../pegainfer-qwen4/tests/frozen_config.json` 的内容（目录里同时保留 tokenizer.json），分别用两种构建各跑一次 `cargo run --release -p pegainfer-server -- --model-path <dir>`，即可复现两个用例断言的那两段输出。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `run_against` 把 stderr 写到文件而不是用 `Stdio::piped()`？

**答案**：管道容量有限（通常 64KB 量级）。用管道时若测试进程在 `wait` 期间不去读，子进程把缓冲写满后会**阻塞在 write 上**、永远退不出，测试就挂在 `try_wait` 循环里直到超时。写文件没有容量上限，子进程总能把 stderr 写完并退出，测试事后再读文件即可——注释原话是「so a full pipe cannot stall the child」（[tests/detect_gate.rs:L54-L56](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L54-L56)）。

**练习 2**：注释说「两次运行暖机后一秒内退出」，`RUN_TIMEOUT` 为什么还要给 60 秒？

**答案**：超时不是给正常路径计时用的，而是兜住**永不返回的回归**——注释原话「this only has to catch a startup or cancellation path that never returns」（[tests/detect_gate.rs:L50-L52](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L50-L52)）。enabled 路径里引擎失败后要把整个 server 级联取消，如果哪天这条取消链断了（server 挂着不死），正常断言根本执行不到，唯一能暴露问题就是这个超时把进程杀掉并 panic 出「已打印的 stderr」。

**练习 3**：如果删掉 `model_dir` 里的 `tokenizer.json`，两个用例各会怎样？

**答案**：stock 用例不受影响——它在 detect 阶段就失败，`launch` 与前端都不会走到，tokenizer 从未被读。enabled 用例则会**断言失败但失败方式变了**：前端没有 tokenizer 会先拒绝启动，stderr 里出现的是 tokenizer 相关错误，既可能不含 `not implemented`（断言直接红），也可能让「引擎拒绝」被顶掉——这正注释里说的「which would surface *that* error instead of the refusal」（[tests/detect_gate.rs:L31-L33](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L31-L33)）。

## 5. 综合实践

把本讲三个模块串成一个**「检测门实验台」**，产出一份可留档的笔记：

1. **三个手造模型目录**：`glm_moe_dsa`（未编译家族）、`frobnicate_lm`（未知家族）、`qwen3 + 假 architectures`（已编译家族的身份错误），构造方法分别见 4.3.4 与 4.1.4。
2. **三次运行 + 输出拆解**：对每条 stderr 输出做「字段级对账」——`NoMatch` 模板里的 `model_type`/`architectures` 各来自 `render` 闭包的哪个分支？rejections 里每一条对应哪条编译线的哪行拒绝代码？提示（若有）对应 `families()` 的哪一行、`find_map` 为什么命中/落空？把每条输出标注成「模板片段 ← 源码位置」的对照表。
3. **端到端交叉验证**：再跑 4.4.4 的两种构建集成测试，回答——你手工触发的 glm52 提示路径与集成测试的 qwen4 提示路径，共享哪几段代码？集成测试比你手工多覆盖了什么（提示：`spawn` 真二进制、stderr 落盘、超时兜底，以及 enabled 侧你手工没测过的 `launch` 拒绝）。
4. **防漂移问答**（书面）：如果把 `families()` 的 glm52 行删掉，哪些测试会失败、哪些行为只会「静默退化」？如果 qwen4 行的 `model_types` 拼错成 `"qwen4exp"`，stock 与 enabled 两个集成测试分别还能过吗？（前半答案：glm52 hint 单测红、无任何集成测试盯 glm52 行——这就是为什么身份表靠单测兜底而非只靠编译器；后半答案：stock 测试会红——提示落空，enabled 测试照过——它走的是 probe 认领，根本不查身份表。）

## 6. 本讲小结

- 检测门只有三种结果：**唯一认领**放行；**`Conflict`**（两条线抢同一配置）立刻返回并点名双方，属注册表 bug；**`NoMatch`** 收集每条编译线的拒绝原因后整体报错，是面向用户的主要失败形态。
- `probe` 是各线自带的身份检查（`model_type` + `architectures` 级别），契约是「恰好一条线认领一份配置」；尺寸与形状校验不属于它。
- `families()` 是「**必要的第二真相源**」：提示服务的对象是被编译掉的线，其代码不在二进制里，身份字符串只能硬编码在 server；`compiled` 由 `cfg!` 编译期决定；表是 **identity-only** 的——量化变体、绑词嵌入、YaRN 也会命中提示，提示不承诺 checkpoint 可服务。
- `feature_gate_hint` 只在 `NoMatch` 时被接线，查顶层 `model_type` 与 `text_config.model_type` 两个键，命中未编译家族即输出 `--features <line>` 重建建议，拼在判定错误之后。
- `detect_gate.rs` 用「冻结真 config + echo-model 最小 tokenizer」驱动**真实二进制**，互斥 `#[cfg]` 的两个用例分别锁住门的两侧：stock 构建的提示路径与 enabled 构建的「认领后 launch 诚实拒绝」路径；两条路径都不触达 CUDA，无 GPU 即可回归。

## 7. 下一步学习建议

- **下一讲进入单元二**：[u2-l1] 把 `probe` 放回它所属的 `ModelLine` trait——8 个方法（`name`/`probe`/`augment_cli`/`consumed_shared_args`/`arg_requirements`/`validate`/`serve_plan`/`launch`）里，本讲只用了前两个和最后一个的失败面。
- 本讲反复出现的 `ModelLineRegistry` 还有另一面——构造期对全部 flag 的 id/拼写唯一性自检与 `validate_provided` 的 consume-or-reject，在 **u2-l3** 展开；`Conflict` 之外的第二类注册表防线就在那里。
- 想预先体会「接入一条新线要动这张表」的完整清单，可跳读 [src/main.rs:L54-L66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L54-L66) 的维护注释与 [tests/detect_gate.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L1-L11) 的模块文档；系统讲解在 **u5-l2**。
- enabled 测试里「引擎失败 → server 级联取消」的双时间线细节属于 vLLM 前端装配，**u4-l1** 详讲；现阶段记住结论即可。
