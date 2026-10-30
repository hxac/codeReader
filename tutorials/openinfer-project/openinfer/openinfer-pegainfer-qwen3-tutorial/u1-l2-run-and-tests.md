# 运行与测试地图：单测、GPU 集成测试与测试夹具

> 本讲属于单元 u1「起步：认识 pegainfer-qwen3」，承接 u1-l1 建立的模块地图与 plan/result 相位边界概念。

## 1. 本讲目标

学完本讲，你应该能够：

1. 分清 pegainfer-qwen3 的两类测试——`--lib` 单元测试（不需要 GPU）与 `tests/` 下的集成测试（需要 CUDA GPU + 模型权重）——并知道如何分别运行它们。
2. 理解 `PEGAINFER_TEST_MODEL_PATH` 环境变量与"clean skip"模式：为什么缺权重时测试是跳过而不是失败。
3. 读懂 `tests/common/harness.rs`：它如何用一个泵线程把引擎的 step 批量输出协议还原成"每个请求一条阻塞流"的旧式测试人体工程学，并能照它的样子写出一个最小集成测试骨架。

## 2. 前置知识

### 2.1 cargo 的两类测试目标

Rust/cargo 把测试分成两类编译目标（target）：

- **单元测试**：写在 `src/` 源码内部、标了 `#[cfg(test)]` 的模块里。它们和库本体一起编译，只在 `cargo test` 时启用，平时不占产物体积。`cargo test --lib` 只编译并运行这一类。
- **集成测试**：`tests/` 目录下每个 `*.rs` 文件都是一个**独立的 crate**，只能通过库的公共 API（`pub` 项）访问被测代码。`cargo test --test <文件名>` 只运行其中一个。

这个区分对本 crate 尤其重要：单元测试测的是纯函数（配置解析、调度装箱、旋钮解析……），**不需要 GPU**；集成测试要真正建起一个 `Qwen3Executor`、装载权重、跑前向，**必须有 CUDA GPU 和模型权重**。

### 2.2 `cfg(test)` 与 feature 的关系

`#[cfg(test)]` 只在编译**库自身测试目标**时为真。集成测试是另一个 crate，它 `use pegainfer_qwen3::...` 时库是以"普通被依赖方"身份编译的，`cfg(test)` 为假。所以"想给集成测试暴露的测试辅助代码"必须走别的门——本 crate 用了一个空 feature `test-fixtures` 来当这扇门，见 4.1.3。

### 2.3 为什么推理引擎的测试要"分层"

模型 crate 的代码里，很大一部分逻辑（配置校验、队列装箱、纯决策级联）是纯 CPU 数据变换，可以在任何机器上快速验证；而数值正确性（logits 是否和 HuggingFace 对得上）和行为契约（取消、前缀缓存命中上报）必须真的在 GPU 上跑。把它们分开意味着：**没有 GPU 的贡献者也能跑通绝大多数回归**，GPU 测试则留给 CI 或本地显卡机器。

### 2.4 你需要的几个背景术语

- **prefill / decode**：u1-l1 已建立的两阶段概念——先整段吃进 prompt（prefill），再逐 token 生成（decode）。
- **teacher-forcing（教师强制）**：解码时不采信模型自己生成的下一个 token，而是喂入预先固定好的"标准答案" token，让每一步的输入完全确定，从而两个引擎（HF 与 pegainfer）在每个位置上看到完全相同的输入，可以逐位置对比分布。
- **bf16 与 ULP**：bfloat16 是截短尾数的浮点格式，一次 GEMM 的求和顺序不同就会让最低几位（Unit in the Last Place，ULP）漂移。这是本讲 4.3 中"为什么不用逐位快照对比"的根源，深度设计留给 u8-l2。
- **生产者-消费者与 Condvar**：一个线程往队列里放数据并 `notify_all`，另一个线程锁住、检查队列、为空则 `wait` 挂起——`harness.rs` 的核心就是这样一个解复用器。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L1-L65) | 声明 features（`test-fixtures`、`kernel-report`）、dev-dependencies 与 feature 门控的二进制 |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L1-L25) | 模块声明与 `test-fixtures` 的 cfg 门；`start_engine` 入口供集成测试调用 |
| [src/spec_env.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/spec_env.rs#L1-L66) | 一个典型的 `#[cfg(test)]` 内联单测样例 |
| [tests/hf_golden_gate.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L1-L687) | HF golden 数值门：直接驱动 executor 的集成测试（本讲的集成测试代表） |
| [tests/common/harness.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L1-L303) | 阻塞式测试夹具：把 step 批量协议还原成每请求阻塞流 |
| [tests/common/mod.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/mod.rs#L1-L41) | 测试公共模块入口：挂载 harness、提供 tokenizer 装载 |
| [tests/cached_tokens_usage.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/cached_tokens_usage.rs#L1-L105) | 一个"骨架级"完整集成测试样例：skip → 启动引擎 → submit → 断言 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**(1) `--lib` 单测入口与 Cargo 测试目标体系**、**(2) `PEGAINFER_TEST_MODEL_PATH` 与 clean skip 模式**、**(3) hf_golden_gate 数值门的测试形态**、**(4) `tests/common/harness.rs` 阻塞式引擎泵**。

### 4.1 `--lib` 单测入口与 Cargo 测试目标体系

#### 4.1.1 概念说明

这个模块回答的问题：**没有 GPU 的机器上，这个 crate 能测什么、怎么测？**

pegainfer-qwen3 的 28 处 `#[cfg(test)]` 模块遍布 `src/`——config（配置校验）、scheduler 与 scheduler/plan（队列装箱、执行计划）、spec_env（环境旋钮解析）、executor/spec（投机解码纯逻辑）、lora（适配器解析）等。它们测的都是**纯数据变换**：输入一个结构体或一段文本，断言输出，完全不碰 CUDA。运行入口就是：

```bash
cargo test --release -p pegainfer-qwen3 --lib
```

注意 `--release`：CLAUDE.md 明确要求所有构建都用 release（debug 下 GPU/CUDA 相关代码极慢，虽然 `--lib` 单测不跑 GPU，但编译整个依赖树时 debug 也慢得多）。

#### 4.1.2 核心流程

`cargo test --release -p pegainfer-qwen3 --lib` 的执行流程：

1. cargo 以 `cfg(test)` 编译 `pegainfer-qwen3` 库目标本身（连带 dev-dependencies，且 dev-dependency 里声明的 feature 一并启用，见下）。
2. 收集所有 `#[test]` 函数，生成测试执行器。
3. 顺序/并行运行，全部不触碰 GPU。

与集成测试的关系：

```text
cargo test --release -p pegainfer-qwen3            # lib 单测 + 所有 tests/*.rs 集成测试
cargo test --release -p pegainfer-qwen3 --lib      # 仅 lib 单测（无 GPU 可跑）
cargo test --release -p pegainfer-qwen3 --test hf_golden_gate   # 仅一个集成测试 crate
```

#### 4.1.3 源码精读

**features 与自引用 dev-dependency。** [Cargo.toml:L34-L45](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L34-L45) 声明了三个 feature：`kernel-call-trace`、`kernel-report`（诊断工具链，拉起一组可选依赖）和 `test-fixtures = []`——一个**空 feature**，唯一作用是当一个编译期开关。[Cargo.toml:L47-L51](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L47-L51) 是关键一招：dev-dependencies 里把**自己**列了进来，并启用 `test-fixtures`：

```toml
[dev-dependencies]
pegainfer-qwen3 = { workspace = true, features = ["test-fixtures"] }
```

这利用了 cargo 的 feature 合并规则：集成测试 crate（`tests/*.rs`）通过这条 dev-dependency 依赖库时，库会带着 `test-fixtures` 编译。库这边对应的门在 [src/lib.rs:L14-L15](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L14-L15)：

```rust
#[cfg(any(test, feature = "test-fixtures"))]
pub use lora::fixtures as lora_fixtures;
```

这行代码的意思：编译库自身测试（`test` cfg 为真）**或** `test-fixtures` feature 打开时，把 LoRA 测试夹具（伪造的适配器权重等）导出为公共 API。于是：单元测试天然可见，集成测试通过 dev-dependency 的 feature 也可见，而普通用户的构建里它根本不存在。

**内联单测样例。** [src/spec_env.rs:L53-L65](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/spec_env.rs#L53-L65) 是最小形态——文件底部一个 `mod tests`，断言"空串/空白/`0` 都解析为 off，非法值报错时要带上变量名"。这是本 crate 单测的平均样子：短、快、无 GPU。

**独立文件的单测组织。** 测试多起来时，本 crate 的惯例是把测试挪到同名文件。[src/scheduler.rs:L578-L581](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/scheduler.rs#L578-L581) 声明了两个只在测试期编译的子模块——`test_support`（测试要用的辅助构造器）和 `tests`（测试本体，位于 `src/scheduler/tests.rs`）。后续 u4 系列讲义会大量阅读这两个文件。

**feature 门控的二进制。** [Cargo.toml:L53-L61](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L53-L61) 声明了 `qwen3_kernel_report` / `qwen3_model_report` 两个诊断二进制，`required-features = ["kernel-report"]` 表示不带 `--features kernel-report` 时它们不会被构建（u8-l3 专门讲）。另外 [Cargo.toml:L2](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L2) 的 `autobenches = false` 关闭了 bench 目标的自动发现，bench 走 `pegainfer-bench` 那条线。

#### 4.1.4 代码实践

**实践目标**：在没有 GPU 的机器上确认 lib 单测可运行、并看清单测都藏在哪些模块里。

**操作步骤**：

1. 在仓库根目录运行 `cargo test --release -p pegainfer-qwen3 --lib`。
2. 再运行 `cargo test --release -p pegainfer-qwen3 --lib -- --list`，把列出的测试按文件名前缀归组。
3. 对照本节列出的 `#[cfg(test)]` 位置（可用 `grep -n "cfg(test)" -r src/` 复核）。

**需要观察的现象**：`--list` 输出里的测试全部来自 `src/` 下的模块（config、scheduler、spec_env、lora……），没有任何一条需要装载权重；测试运行总时长很短（工作区文档对整套 `--workspace --lib` 的标注约 9 秒）。

**预期结果**：全部通过。**待本地验证**（本讲义写作环境未执行 cargo）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `test-fixtures` 必须是 feature，而不能直接 `#[cfg(test)]`？
**答案**：`cfg(test)` 只在编译库自身的测试目标时为真；`tests/` 下的集成测试是独立 crate，它看到的库是普通依赖，`cfg(test)` 为假。空 feature + dev-dependency 自引用（[Cargo.toml:L48](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/Cargo.toml#L48)）让集成测试编译时该门为真，而用户构建不受影响。

**练习 2**：`cargo test -p pegainfer-qwen3`（不带 `--lib`）和 `--lib` 的区别是什么？
**答案**：前者还会编译并运行 `tests/` 下全部 24 个集成测试文件；这些测试大多需要 GPU + 权重，缺权重时它们会走 skip 分支（见 4.2）而非失败，但编译整个测试集仍比 `--lib` 慢。

**练习 3**：`src/scheduler.rs` 里 `test_support` 与 `tests` 两个子模块为什么不直接内联在 `scheduler.rs` 里？
**答案**：把测试与测试辅助拆到 `src/scheduler/tests.rs` / `test_support.rs` 独立文件，避免主源码文件被几百行测试代码撑爆；`#[cfg(test)]` 保证它们不进入正式产物。这是大模块（如 scheduler、weights）的通用惯例。

### 4.2 `PEGAINFER_TEST_MODEL_PATH` 与集成测试的 clean skip 模式

#### 4.2.1 概念说明

这个模块回答的问题：**集成测试怎么做到"有 GPU + 权重就真跑，没有就干净跳过"？**

答案是一个在本 crate 23 个集成测试文件中重复出现的函数模式 `model_path_or_skip()`，配合环境变量 `PEGAINFER_TEST_MODEL_PATH`（也是 CLAUDE.md 列出的官方测试旋钮）。它的三段式逻辑是：

1. 环境变量设了 → 用它作为模型目录；
2. 没设 → 探测默认路径 `<crate>/../models/Qwen3-4B`（相对 `CARGO_MANIFEST_DIR`，即 crate 根的上一级 `models/` 目录）是否有 `config.json`；
3. 都没有 → `eprintln!` 打印一句 skip 说明，返回 `None`，测试函数直接 `return`。

关键取舍：**skip 而不是 fail，也不是 `#[ignore]`**。skip 之后的测试会被 cargo 记为通过，但 stderr 上留下一行可检索的说明，贡献者知道这条测试存在、只是本机没权重；而 `#[ignore]` 会让测试从默认运行中彻底消失，容易被遗忘。

#### 4.2.2 核心流程

```text
#[test] fn xxx() {
    let Some(model_path) = model_path_or_skip() else { return; };   // ← 每个 GPU 测试的第一行
    // ……启动引擎、跑请求、断言（只有拿到 model_path 才会走到这里）
}
```

`model_path_or_skip()` 内部：

```text
读取 PEGAINFER_TEST_MODEL_PATH
├─ Ok(path)                     → Some(path)
├─ Err 且 <manifest>/../models/Qwen3-4B/config.json 存在 → Some(默认路径)
└─ Err 且默认路径不存在          → eprintln!("skipping qwen3 xxx: …")；None
```

#### 4.2.3 源码精读

**标准实现。** [tests/cached_tokens_usage.rs:L24-L37](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/cached_tokens_usage.rs#L24-L37) 是这个模式的最干净样例：`match std::env::var(...)` 的三臂分别对应上面流程图的三个分支。默认路径常量在 [tests/cached_tokens_usage.rs:L21](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/cached_tokens_usage.rs#L21)，用 `concat!(env!("CARGO_MANIFEST_DIR"), "/../models/Qwen3-4B")` 在编译期拼出绝对路径——不依赖当前工作目录，测试从任何目录启动都有效。调用侧在 [tests/cached_tokens_usage.rs:L57-L59](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/cached_tokens_usage.rs#L57-L59)：`let Some(model_path) = model_path_or_skip() else { return; }`——let-else 一行完成"跳过即返回"。

**同一函数在 hf_golden_gate 里的版本。** [tests/hf_golden_gate.rs:L116-L129](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L116-L129) 与上一段逐字同构，只是 skip 文案换成该测试自己的名字。

**skip 的第二层：golden 夹具缺失。** hf_golden_gate 还需要第二份输入——HF 参考分布夹具。夹具按模型尺寸命名，[tests/hf_golden_gate.rs:L138-L159](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L138-L159) 用 `(hidden_size, num_hidden_layers)` 几何查尺寸表（如 `(2560, 36)` → `"4b"`）；[tests/hf_golden_gate.rs:L291-L321](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L291-L321) 在几何查不到、或该尺寸的 `test_data/qwen3-{size}-hf-golden.safetensors` 不存在且不在已提交清单里时，同样打印说明并返回 `None`（若设了 `PEGAINFER_QWEN3_HF_GOLDEN` 环境变量则强制从该路径读，查不到几何直接 assert 失败——显式指定却对不上，是配置错误而非缺资源）。

**skip 的第三层：GPU 数量。** [tests/hf_golden_gate.rs:L668-L673](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L668-L673) 在可见 CUDA 设备少于 2 个时跳过 TP=2 对照组（设备数探测在 [tests/hf_golden_gate.rs:L543-L545](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L543-L545)，驱动都查不到时按 0 处理）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认"无权重时集成测试 skip 而不 fail"。

**操作步骤**：

1. 确认没有设置 `PEGAINFER_TEST_MODEL_PATH`，且 `<仓库>/models/Qwen3-4B/config.json` 不存在。
2. 运行 `cargo test --release -p pegainfer-qwen3 --test cached_tokens_usage -- --nocapture`。
3. 把某个含 `config.json` 的目录指给环境变量，重跑同一条命令。

**需要观察的现象**：第 2 步 stderr 打印 `skipping qwen3 cached_tokens_usage: ... set PEGAINFER_TEST_MODEL_PATH to run it`，测试结果为 ok（函数提前返回）；第 3 步则会真正尝试装载（随后可能因目录里没有完整权重而失败——这是预期行为，说明它已越过 skip 分支）。

**预期结果**：skip 分支零失败。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么默认模型路径用 `CARGO_MANIFEST_DIR` 拼接，而不是相对路径 `"../models/Qwen3-4B"`？
**答案**：集成测试二进制的运行时工作目录不一定是 crate 根（cargo 以 crate 根为 cwd 运行测试，但 `--target-dir`、外部调用等情况下不保证）；`env!("CARGO_MANIFEST_DIR")` 在编译期固化为绝对路径，与运行目录无关（见 [tests/cached_tokens_usage.rs:L21](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/cached_tokens_usage.rs#L21)）。

**练习 2**：`model_path_or_skip` 探测的是 `config.json` 存在性而不是 `*.safetensors`，会不会出现"探测通过但后面装载失败"？
**答案**：会。`config.json` 是"这里像一份模型"的最低门槛（hf_golden_gate 还用它读几何，见 L138 起），完整权重缺失会在引擎启动阶段以错误暴露。skip 只负责"明显没模型"这一层，目录残缺属于应该报错的场景。

**练习 3**：为什么不把这些测试标 `#[ignore]`，要用运行时 skip？
**答案**：`#[ignore]` 的测试默认根本不运行，久了没人记得开；运行时 skip 让测试始终出现在结果里，且 stderr 说明"怎样才能跑起来"，对新贡献者是一份活文档。

### 4.3 hf_golden_gate 数值门的测试形态：绕过调度器，直接驱动 executor

#### 4.3.1 概念说明

这个模块回答的问题：**一个"数值正确性"集成测试长什么样，它和其他集成测试的驱动方式有何本质不同？**

`tests/hf_golden_gate.rs` 是 Qwen3 的精度门：以 HuggingFace 为数值真值，把固定 token 序列 teacher-forcing 地回放进 pegainfer，逐位置对比 next-token logprobs。它的关键形态特征是——**完全不用 harness、不用调度器、不用 frontend**，直接拿 u1-l1 讲过的 `runtime` 再导出层：

```rust
use pegainfer_qwen3::runtime::{DecodePlan, DecodeStepItem, PrefillPlan, PrefillStepItem, Qwen3Executor, RequestId};
```

这印证了 u1-l1 的结论：`runtime` 再导出（[src/lib.rs:L202-L219](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L202-L219)）是本 crate 的公共执行边界——scheduler 和测试都以 plan/result 纯数据与 `Qwen3Executor` 对话。文件头部的长注释（[tests/hf_golden_gate.rs:L1-L53](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L1-L53)）本身就是一份设计文档，值得整段通读。

容差常量的深设计（为什么 mean/p99 而不是 max、margin 如何避开真平局）是 u8-l2 的主题，本讲只建立形态认知。

#### 4.3.2 核心流程

一次 teacher-forcing 回放（`run` 函数的 batched 分支）：

```text
prefill: PrefillPlan { requests: [PrefillStepItem::new(id, prompt, …, Some(LOGPROBS))] }
         → execute_prefill → 每序列取 first_token_logprob，与 HF 位置 0 对比
循环 decode_len 次:
    每序列喂 golden 自己的 decode token（teacher-forcing）
    DecodePlan { requests: [DecodeStepItem::new(id, fed, …)] }
         → execute_decode → 每序列取 logprob，与 HF 位置 step+1 对比
收尾: 对每个 id 调 drop_request 释放 KV
```

主测试函数把多个"pass"塞进**一个** `#[test]` 里顺序执行：bs=1 顺序回放（+ 重复一次做逐位确定性断言）、batched eager（跨请求隔离检查）、prefix-cache 冷/热回放、CUDA-graph 桶跨越批（batch 9→桶 16、5→桶 8，最大化 padding 槽位）、以及 ≥2 GPU 时的 TP=2 全套重放。

#### 4.3.3 源码精读

**构造 plan item 的两个小工厂。** [tests/hf_golden_gate.rs:L374-L387](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L374-L387)：`prefill_item` / `decode_item` 把 prompt（或被喂入的 token）包成 `PrefillStepItem` / `DecodeStepItem`，都请求 `Some(LOGPROBS)`（64 个 top logprob，让 pegainfer 的 top-K 与 HF 夹具的 top-K 有交集）。

**回放主循环。** [tests/hf_golden_gate.rs:L396-L486](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L396-L486) 的 `run` 是核心：`batched = false` 逐序列单独跑（bs=1，最紧对比），`batched = true` 时先给所有序列发 prefill（L413-L418 直接调 `ex.execute_prefill(PrefillPlan { sample_seed: 0, requests: &items })`），然后每步给所有序列发 decode（L434-L439），teacher-forcing 体现在 L432——喂的是 `g.decode(s, step)`，即 golden 夹具自己的第 `step` 个"标准答案" token，不是 pegainfer 采样的结果。函数还顺手返回一个 fingerprint（pegainfer 每个位置自己的 top-1 logprob），供外部断言两次同输入运行**逐位相同**（[L564-L570](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L564-L570)）。

**为什么只有一个 `#[test]`。** [tests/hf_golden_gate.rs:L606-L634](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L606-L634)：注释写明"每个 executor 独占 GPU 显存；用作用域保证同时只有一个模型驻留……所有内容放在一个 `#[test]` 里也是同一原因：两个并行测试会各自为 KV 预算 85% 的空闲显存"。cargo 默认并行跑测试线程，对会瓜分显存的 GPU 测试来说并行=自杀，所以整个门是一根顺序执行的长链。

**图 pass 的资格门。** [tests/hf_golden_gate.rs:L639-L663](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L639-L663)：CUDA-graph 回放只在"该模型的 GQA group 有编译好的解码核"时执行（判定函数在 [L168-L185](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L168-L185)，查 `pegainfer_core::ops::SUPPORTED_GQA_GROUP_SIZES`）；否则打印说明、跳过图 pass（例：Qwen3-14B 的 group 5 未编译）。

#### 4.3.4 代码实践

**实践目标**：不运行、只靠阅读，验证你理解 teacher-forcing 回放的数据流。

**操作步骤**：

1. 通读 [tests/hf_golden_gate.rs:L396-L486](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L396-L486)，画出 batched 分支的消息流：`PrefillPlan{requests:[…]} → execute_prefill → PrefillResult`，循环内 `DecodePlan{requests:[…]} → execute_decode → DecodeResult`。
2. 回答：位置 `step + 1` 的对比用的 pegainfer 数据来自哪个字段、HF 数据来自 `g.topk(s, pos)` 的哪一 rank。
3. 找出 `drop_request` 在两个分支里的调用位置，解释为什么测试结束必须显式丢弃请求。

**需要观察的现象 / 预期结果**：你的图里每个位置恰好产生一次对比；`drop_request` 在 batched 分支是循环逐个 id（[L449-L451](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L449-L451)）、在 bs=1 分支是每序列跑完即丢（[L482](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L482)）——executor 的 KV 预算不会因为测试累积的请求而泄漏。

**练习 3 答案提示**：`drop_request` 释放该请求在 executor 侧的 KV 与解码槽位；不释放则显存被占满，后续 pass 再建 executor 就会失败。

#### 4.3.5 小练习与答案

**练习 1**：teacher-forcing 与自由贪心对比，各自适合验证什么？
**答案**：自由贪心一旦某步 argmax 翻转，两条序列从此分岔，只能比最终文本，定位不了出错位置；teacher-forcing 每步喂同一固定 token，两个引擎在每个位置输入完全相同，可以逐位置断言分布（margin/mean/p99）。它牺牲了"端到端生成质量"的覆盖，换取可归因的数值定位。

**练习 2**：为什么 `LOGPROBS` 要请求 64 个而不是 1 个？
**答案**：见 [tests/hf_golden_gate.rs:L72-L75](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L72-L75) 的注释——对比只依赖头部，但更宽的 top-K 让"pegainfer 选的 token 是否在 HF 的 top-K 里"这类推理更稳健（argmax 违规检查需要 pegainfer 的选择能映射进 HF 的名单，见 [L253-L263](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L253-L263)）。

**练习 3**：主测试里 eager pass 之后、graph pass 之前为什么要 `drop` 掉 eager executor（借助作用域）？
**答案**：每个 executor 按空闲显存的一定比例预留 KV 池；同时存在两个 executor 会重复瓜分显存（注释见 [L630-L634](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/hf_golden_gate.rs#L630-L634)）。用块作用域让前一个 executor 离开作用域、释放显存后再建下一个。

### 4.4 `tests/common/harness.rs`：把 step 批量协议还原成每请求阻塞流

#### 4.4.1 概念说明

这个模块回答的问题：**集成测试怎么方便地"提交一个请求、阻塞等它生成完"？**

引擎契约（pegainfer-frontend 的 `Engine`）对外吐的是**按 step 批量**的更新流：每个 step 一批 `RequestUpdate`，不同请求的更新混在同一批里。而集成测试的人体工程学是老的"每请求一条流"——`submit` 之后逐条 `recv`，直到 `Finished`。[tests/common/harness.rs:L1-L7](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L1-L7) 的文件头注释说得直白：集成测试早于 step 批量协议存在，这个泵用**生产桥同款方式**（一个线程把 `StepOutputs` 解复用进每请求队列）恢复了旧人体工程学，且全程同步——GPU 测试就在普通测试线程上驱动请求。

为什么这个文件放在 `tests/common/` 且每个测试文件都要 `mod common;`？因为 cargo 的集成测试每个文件是独立 crate，公共代码只能以模块源码的形式被各自编译一次（[tests/common/mod.rs:L1](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/mod.rs#L1) 的 `pub(crate) mod harness;`）。[tests/common/harness.rs:L9-L11](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L9-L11) 因此标了 `#![allow(dead_code)]`——每个测试 crate 只用这个模块的一部分，未用的部分不该报警。

#### 4.4.2 核心流程

夹具的生命周期：

```text
EngineHarness::new(engine)
    ├─ 断言 engine.schedulers.len() == 1（测试只驱动单调度器引擎）
    ├─ scheduler.handle.take_steps()   ← 每个 scheduler 的 step 流只能被取走一次
    ├─ 建立 Inbox { updates: HashMap<RequestId, VecDeque<RequestUpdate>>, closed } + Condvar
    └─ spawn 泵线程:
         while let Some(step) = steps.blocking_recv():
             把 step.updates 按 update.id 分发进各自队列 → notify_all
         流关闭后: closed = true → notify_all

submit(request) → RequestStream { id, control, inbox }
RequestStream::recv()  → 锁 Inbox；自己队列有则弹出，否则在 Condvar 上等待
RequestStream::outcome() → 反复 recv，折叠 tokens/logprobs，直到 terminal
RequestStream::expect_finished() → outcome + 要求 Terminal::Finished，否则 panic

Drop（夹具销毁）
    ├─ drop(handle)  → 关闭提交通道，scheduler 排干并退出
    ├─ join(scheduler线程) → step 流随之关闭
    └─ join(泵线程)        → 泵检测 closed，退出
```

#### 4.4.3 源码精读

**默认请求工厂。** [tests/common/harness.rs:L37-L65](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L37-L65)：`request(prompt_tokens, params, max_tokens)` 填一份"测试默认"的 `Request`——无 LoRA、无 logprobs、无历史、EOS 策略随采样参数。注释点明用法：要测 prompt-logprob / LoRA 变体，就在返回值上改字段。

**夹具结构。** [tests/common/harness.rs:L67-L74](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L67-L74)：`EngineHarness` 持有 scheduler 句柄、LoRA 客户端、scheduler 与泵两个线程的 JoinHandle、共享 Inbox，以及引擎的 `EngineInfo`。

**构造与泵线程。** [tests/common/harness.rs:L87-L130](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L87-L130)：`new` 先断言单调度器（L88-L92），`take_steps()` 拿走 step 流（L94-L97，"新鲜的 scheduler 只让出一次"），然后 spawn 泵线程（L106-L121）——`blocking_recv` 收一个 step，把其中每条 update 按其 `id` 塞进 `HashMap` 里对应的 `VecDeque`，解锁后 `notify_all`；流结束时置 `closed = true` 再 `notify_all`。

**提交与便捷方法。** [tests/common/harness.rs:L132-L143](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L132-L143)：`submit` 经 scheduler 句柄提交请求，返回绑定了请求 id 与 Inbox 的 `RequestStream`。[tests/common/harness.rs:L160-L169](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L160-L169) 的 `generate` 是"大多数 GPU 测试要的形状"：提交、折叠到 Finished、直接返回 token 序列。

**每请求视图。** [tests/common/harness.rs:L200-L215](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L200-L215)：`recv` 是教科书式的 Condvar 循环——锁内先试弹出自己的队列，弹到就返回；`closed` 就返回 `None`；否则 `wait` 让出锁等待泵的 `notify_all`。`RequestStream` 是 `Send` 的（L186-L187 注释），所以并发请求可以从不同测试线程各自驱动。

**折叠与终止。** [tests/common/harness.rs:L219-L262](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L219-L262)：`outcome` 循环 recv，把 tokens / logprobs 累积进 `Outcome`（结构见 [L267-L273](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L267-L273)），并断言 `cached_tokens` 至多上报一次（L237-L243——这本身就是在钉一条引擎契约，u5-l1 的 cached_tokens_usage 测试依赖它）；拿到 `terminal` 即返回。`expect_finished` 在 L255-L262：`Rejected`/`Failed` 一律 panic——对测试而言"引擎拒绝"不是一种可接受的结果，而是一次失败。

**有序关闭。** [tests/common/harness.rs:L172-L184](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L172-L184)：`Drop` 的注释把因果链写全了——关提交通道 → scheduler 排干退出 → step 流关闭 → 泵收尾。顺序 join 两个线程，保证测试进程不会留下悬挂线程。

**日志开关。** [tests/common/harness.rs:L279-L302](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L279-L302)：cargo 的测试二进制默认不装 logger；`init_capture_logging` 在 `PEGAINFER_TEST_LOG=1` 时装一个只转发 `pegainfer_qwen3` 目标的最小 stderr logger——投机解码的负载门测试（`tests/dflash_speculative_gate.rs`，u6-l7）靠数子进程 stderr 上的 hedge trace 行来断言，多余的日志会污染解析。

**一个完整的调用方样例。** [tests/cached_tokens_usage.rs:L55-L104](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/cached_tokens_usage.rs#L55-L104) 展示了夹具的完整用法链：`model_path_or_skip()` → `pegainfer_qwen3::start_engine(Path::new(&model_path), EngineLoadOptions { enable_cuda_graph: true, device_ordinals: vec![0], seed: 42, .. })`（`start_engine` 定义在 [src/lib.rs:L354-L366](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L354-L366)，它只是带默认参数转发到 `start_engine_with_offload`）→ `EngineHarness::new(engine)` → `common::load_tokenizer(&model_path)` 编码 prompt → `engine.submit(request(...)).expect_finished()` 折叠结果并断言 `cached_tokens`。tokenizer 装载在 [tests/common/mod.rs:L14-L40](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/mod.rs#L14-L40)，按模型目录自动挑 HF / Tiktoken / Tekken 三种后端。

#### 4.4.4 代码实践

**实践目标**：在纸面上跟踪一次 `generate` 调用的完整数据流，确认你理解泵的存在意义。

**操作步骤**：

1. 从 [tests/common/harness.rs:L160-L169](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L160-L169) 的 `generate` 出发，依次列出涉及的结构：`request()` → `submit()` → `RequestStream` → `recv()`/`outcome()` → `Outcome.tokens`。
2. 画一张时序图，三条竖线：测试线程、泵线程、scheduler。标出 `submit`、每个 step 的 `blocking_recv` → 队列分发 → `notify_all` → 测试线程 `wait` 醒来取数据、以及 `Terminal` 到达和夹具 `Drop` 时的关闭顺序。
3. 思考并回答：如果没有泵线程、测试线程直接 `blocking_recv` step 流，会发生什么？

**需要观察的现象 / 预期结果**：第 3 步的答案——测试线程会拿到**所有请求混在一起**的批量更新，得自己按 id 过滤；请求一多，"等某个特定请求的下一条"没有阻塞原语可用。泵把"按 id 分发 + 等待"封装进 `Inbox`+`Condvar`，测试侧就回到每请求一条流的直觉。

#### 4.4.5 小练习与答案

**练习 1**：`EngineHarness::new` 为什么断言 `engine.schedulers.len() == 1`？
**答案**：夹具只从一个 scheduler 取 step 流、Inbox 也只解复用这一条流；多调度器（多模型线/多设备拓扑）的引擎不适用这个简化夹具（[tests/common/harness.rs:L88-L92](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L88-L92)）。

**练习 2**：`recv` 在什么情况下返回 `None`？`outcome` 为什么把它当作 panic 而不是正常结束？
**答案**：`None` 只在 `closed == true`（引擎侧流已关闭）时出现（[L210-L212](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L210-L212)）。正常路径下每个请求必以一个 `terminal` 更新收尾；流先于 terminal 关闭意味着引擎死掉，对测试是失败而非结果（`outcome` 里的 `expect`，[L232-L234](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L232-L234)）。

**练习 3**：`Drop` 里如果先 join 泵线程、后 drop handle，会发生什么？
**答案**：泵线程在 `blocking_recv` 上等下一个 step，而 scheduler 要等提交通道关闭才会排干退出——通道由 `handle` 持有，不先 drop 它泵永远收不到流关闭，join 死锁。所以顺序必须是 drop handle → join scheduler → join 泵（[L172-L184](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/common/harness.rs#L172-L184) 注释原文即此因果链）。

## 5. 综合实践：写一个最小集成测试骨架

**任务**：通读 `tests/common/harness.rs` 与任一集成测试的 skip 分支后，亲手写一个最小集成测试骨架——复用 harness 的 submit/pump 语义，无 GPU 时 clean skip——并验证它编译通过、且不破坏 crate 的 lib 目标。

**步骤 1：通读两份源码**。重点行号：harness 的 `new`/`submit`/`expect_finished`/`Drop`（4.4.3 已列），以及 [tests/cached_tokens_usage.rs:L24-L37](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/cached_tokens_usage.rs#L24-L37) 的 skip 分支。

**步骤 2：在 `tests/` 下新建骨架文件**（名字自取，例如 `tests/smoke_skeleton.rs`；这是你的本地练习文件，不要提交到仓库）。以下为**示例代码**（仿照 `cached_tokens_usage.rs` 的形状，非项目原有代码）：

```rust
//! Minimal integration-test skeleton: reuses the harness's submit/pump
//! semantics and skips cleanly when no model weights are present.

use std::path::Path;

use pegainfer_frontend::engine::EngineLoadOptions;
use pegainfer_frontend::sampler::SamplingParams;

mod common;

use common::harness::EngineHarness;

const MODEL_PATH: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../models/Qwen3-4B");

fn model_path_or_skip() -> Option<String> {
    match std::env::var("PEGAINFER_TEST_MODEL_PATH") {
        Ok(path) => Some(path),
        Err(_) if Path::new(MODEL_PATH).join("config.json").exists() => {
            Some(MODEL_PATH.to_string())
        }
        Err(_) => {
            eprintln!(
                "skipping qwen3 smoke_skeleton: {MODEL_PATH}/config.json is missing; set PEGAINFER_TEST_MODEL_PATH to run it"
            );
            None
        }
    }
}

#[test]
fn short_generation_finishes_with_tokens() {
    let Some(model_path) = model_path_or_skip() else {
        return;
    };
    let engine = EngineHarness::new(
        pegainfer_qwen3::start_engine(
            Path::new(&model_path),
            EngineLoadOptions {
                enable_cuda_graph: true,
                device_ordinals: vec![0],
                seed: 42,
                ..EngineLoadOptions::default()
            },
        )
        .expect("failed to start engine"),
    );
    let tokenizer = common::load_tokenizer(&model_path);
    let prompt = "The quick brown fox";
    let tokens = tokenizer.encode(prompt, false).expect("encode failed");
    let out = engine.generate(tokens, SamplingParams::default(), 8);
    assert!(!out.is_empty(), "a non-empty prompt must generate tokens");
}
```

骨架的四个要点与 4.2/4.4 一一对应：skip 三段式（环境变量 → 默认路径探测 → 打印并返回 `None`）；let-else 提前返回；`start_engine` + `EngineHarness::new` 的装配顺序；`generate` 走 submit → 泵 → `expect_finished` 的完整链路。

**步骤 3：验证编译**。按讲义规格先确认 crate 的 lib 目标不受影响，再编译骨架目标本身：

```bash
cargo test --release -p pegainfer-qwen3 --lib            # lib 目标仍编译、单测仍通过
cargo test --release -p pegainfer-qwen3 --test smoke_skeleton --no-run   # 骨架自身编译通过
```

**步骤 4：观察两种运行形态**：

- 无权重（不设 `PEGAINFER_TEST_MODEL_PATH` 且 `models/Qwen3-4B` 缺失）：`cargo test --release -p pegainfer-qwen3 --test smoke_skeleton -- --nocapture`，预期 stderr 打出 skip 行、测试记为通过；
- 有 GPU + 权重：同一命令真正启动引擎、生成 8 个 token 上限内的序列（EOS 可能提前终止，所以断言只要求非空）。

**预期结果**：编译零错误；skip 形态零失败。**待本地验证**（本讲义写作环境无 GPU，未执行）。

## 6. 本讲小结

- pegainfer-qwen3 的测试分两层：`--lib` 单测（`src/` 内 28 处 `#[cfg(test)]`，纯 CPU 逻辑，无 GPU 可跑）与 `tests/` 下 24 个集成测试文件（每个是独立 crate，需要 GPU + 权重）；`test-fixtures` 空 feature 经 dev-dependency 自引用把测试夹具（如 `lora_fixtures`）只暴露给测试编译。
- 集成测试统一采用 `model_path_or_skip()` 模式：`PEGAINFER_TEST_MODEL_PATH` 覆盖 → `<manifest>/../models/Qwen3-4B/config.json` 探测 → 打印说明并提前返回；缺资源是 skip，不是 fail。
- `tests/hf_golden_gate.rs` 展示另一种驱动形态：绕过调度器与 frontend，直接用 `runtime` 再导出的 `Qwen3Executor` + plan/result 类型做 teacher-forcing 数值回放，全部 pass 串在一个 `#[test]` 里以避免显存被并行测试重复瓜分。
- `tests/common/harness.rs` 用一个泵线程把 step 批量更新解复用进每请求队列（`Inbox` + `Condvar`），向测试暴露 `submit`/`recv`/`outcome`/`expect_finished` 的阻塞流人体工程学；`Drop` 按 handle → scheduler → 泵的顺序有序关闭。
- `PEGAINFER_TEST_LOG=1` 可给测试二进制装一个只转发本 crate 日志的最小 logger，供需要解析 stderr 的测试（如 dflash 负载门）使用。

## 7. 下一步学习建议

- **下一讲 u1-l3（ModelLine：Qwen3 如何挂进服务器 CLI）**：本讲只用了 `start_engine` 这个高层入口，下一讲沿相反方向看 crate 如何通过 `Qwen3Line` 的 probe/validate/launch 钩子接入服务器 CLI，并了解跨标志互斥规则的集中地 `src/model_line.rs`。
- 想先看更多测试形态，可以浏览 `tests/scheduler_robustness.rs` 与 `tests/context_window.rs`（u9-l2 会系统巡礼行为测试族），以及 `tests/dflash_speculative_gate.rs` 里 `init_capture_logging` 的实际调用点（u6-l7）。
- hf_golden_gate 的容差设计（margin/mean/p99 为什么长这样）在 **u8-l2** 专讲，读完调度器与前向（u3/u4）再回看会更有体感。
