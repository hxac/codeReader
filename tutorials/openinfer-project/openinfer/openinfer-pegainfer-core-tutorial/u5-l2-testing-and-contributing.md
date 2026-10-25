# u5-l2 测试策略与二次开发：向 pegainfer-core 贡献一个改动

## 1. 本讲目标

这是本手册的收官一讲。前面 u3 精读了页池与 KV 池，u5-l1 把它们的 RAII、两段式准入上升为并发正确性纪律；本讲回答最后一个问题：**当你要改 pegainfer-core 的一行代码时，如何证明它是对的，以及如何把它变成一次符合仓库规范的贡献。**

学完本讲你应该能够：

1. 为一处 core 改动选择正确的验证层级（纯 CPU 单测 / GPU 单测 / 集成测试 / 模型 crate 精度门），判据是「改动触碰了什么」。
2. 按仓库规范组织一次完整贡献：功能分支、`cargo fmt`、Commitizen 格式的提交信息，并能自查「每个改动文件服务于同一不变量」。
3. 识别被仓库明令禁止的做法——「模拟或源码文本测试冒充生产证据」。

## 2. 前置知识

本讲默认你已读完：

- **u1-l3（构建、依赖与测试）**：core 的 43 个内嵌单测分布在 12 个源文件里；GPU 隔离不靠 `#[ignore]`（全 crate 搜不到这个属性），而靠「命令分层 + 运行时 `expect` 显式红灯」。
- **u3-l1（page_pool）**：`PagePool` 是带锁 free-list 的固定页分配器，`OwnedPagePermit` 用 Drop 语义自动还页。
- **u5-l1（并发正确性）**：`try_reserve`（预约半段）→ `commit_reservation`（提交半段）的两段式准入，以及 padding 页在构造期预留的确定性纪律。

再补三个通用概念：

- **就地内嵌单测 vs 集成测试**：Rust 惯例是把 `#[cfg(test)] mod tests` 写在被测文件内部，只能看见 crate 内部视角；`tests/` 目录下的文件则被编译成独立的集成测试二进制，以**外部 crate 视角**（`use pegainfer_core::…`）使用被测库，只能触到 `pub` API。
- **精度门（accuracy gate）**：对拍固定基准数据的测试。core 的单测守护的是结构不变量（页守恒、stride 几何），不产出 token；数值正确性的证据必须在模型 crate 层产生（对拍 HuggingFace golden logits）。
- **Commitizen**：一种提交信息格式约定 `<type>(<scope>): <subject>`，本仓库强制使用（详见 4.3）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| `src/page_pool.rs`（194 行） | 固定页分配器 + RAII 许可 | 纯 CPU 测试样板；`absorb` 是本讲演练的「未被覆盖分支」 |
| `src/kv_pool.rs`（563 行） | page-first KV 布局、两段式准入 | 展示纯 CPU / GPU 两类测试的拆分，以及 bail 路径的覆盖情况 |
| `tests/weight_loader_gpu.rs`（77 行） | core 唯一的集成测试（需 GPU） | 归纳它测什么、为什么必须独占文件 |
| `Cargo.toml`（39 行） | 依赖、feature、lints 配置 | dev-dependency 与 `[lints] workspace = true` |
| `src/lib.rs`（15 行） | 13 个 `pub mod` 的门面 | 确认 crate 公开面 |
| `CLAUDE.md`（仓库根目录） | 贡献规范、测试命令、环境变量的权威来源 | 4.2、4.3 的规范依据（行号待确认，见 4.2.3 的说明） |

永久链接统一使用当前 HEAD `09a1a38f`。

## 4. 核心概念与源码讲解

### 4.1 测试分层：43 + 2 的三层结构

#### 4.1.1 概念说明

用 `grep -c '#\[test\]'` 实测（当前 HEAD），core 的测试分布是：

| 文件 | 内嵌 `#[test]` 数 | 是否需要 GPU |
| --- | --- | --- |
| `src/kv_pool.rs` | 6 | 2 个否（stride 几何）+ 4 个是 |
| `src/page_pool.rs` | 3 | 否（纯 `Mutex` 分配器，无设备对象） |
| `src/cuda_graph/dump/render.rs` | 7 | 未逐一验证 |
| `src/cuda_graph/dump.rs` | 5 | 未逐一验证 |
| `src/logging.rs` | 4 | 否（宿主侧日志配置） |
| `src/rope.rs` | 4 | 未逐一验证 |
| `src/cpu_topology.rs` | 3 | 未逐一验证 |
| `src/decodable_vocab.rs` | 3 | 否（tokenizer schema 解析） |
| `src/weight_loader.rs` | 3 | 未逐一验证 |
| `src/eos.rs` | 2 | 否（检查点文件解析） |
| `src/weight_loader/staging.rs` | 2 | 未逐一验证 |
| `src/ops/call_trace.rs` | 1 | 未逐一验证 |
| **合计** | **43 / 12 个文件** | |
| `tests/weight_loader_gpu.rs` | 2（集成） | 是 |

分层的关键不是「在哪个目录」，而是**测试构造了什么**：

1. **纯 CPU 内嵌单测**（快车道）：只构造值类型和宿主侧数据结构。
2. **GPU 内嵌单测**：同样写在 `#[cfg(test)] mod tests` 里，但需要 `DeviceContext`。它们**没有** `#[ignore]` 属性——在无 GPU 的机器上会以 `expect` 显式 panic，这是仓库 deliberate 的选择（u1-l3 已纠正过早期规划里「`#[ignore]` 隔离」的说法，本讲沿用源码事实）。
3. **GPU 集成测试**：`tests/weight_loader_gpu.rs`，以外部 crate 视角测 `pub` API 的状态机契约。

再往上一层是**模型 crate 的精度门**（4.2），它不在 core 内，但 core 改动的最终证据往往落在那里。

#### 4.1.2 核心流程

为一个改动选验证层级，判据是「改动落点触碰了什么」：

```text
改动落点？
├─ 纯值类型 / 纯宿主逻辑（KvLayout、pages_needed、PagePool）
│    └─ 就地 #[cfg(test)] 单测 → 进 cargo test --lib 快车道
├─ 持有设备对象的类型（KvPool/KvState/StagedWeightLoader → CudaSlice）
│    └─ 就地单测 + DeviceContext::new().expect(...) 显式红灯
├─ pub API 契约 / 消费方依赖的状态机
│    └─ tests/ 集成测试（外部 crate 视角，--lib 不编译它）
└─ 影响前向数值输出
     └─ 模型 crate 精度门（hf_golden_gate / e2e_scheduler，见 4.2）
```

以 kv_pool 为例，判据在类型定义处就写明了：`KvLayout` 的文档注释自证「纯值类型，无 GPU、无分配」，所以 stride 测试免 GPU；`KvPool` 持有 `CudaSlice<bf16>`，构造必须走 `alloc_zeros`，所以生命周期测试需要 GPU。两条测试命令因此分层：

```bash
# 快车道：含 2 个纯 CPU 的 stride 测试（无 GPU 机器可过）
cargo test --release -p pegainfer-core --lib

# 按名进入 GPU 测试（无 GPU 机器会显式 panic）
cargo test --release -p pegainfer-core --lib kv_state_lifecycle -- --nocapture

# 集成测试：--lib 不编译 tests/ 目录，必须按名点名
cargo test --release -p pegainfer-core --test weight_loader_gpu
```

（以上行为均为 cargo 标准语义 + 源码可证，具体在本机的表现待本地验证。）

#### 4.1.3 源码精读

**① 纯 CPU 与 GPU 的分界写在类型文档里。** [src/kv_pool.rs:15-33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L15-L33)：`KvLayout` 的注释明确「Pure value type — no GPU, no allocation. Shared by `KvPool`, `KvState`, `KvDesc`.」，字段是 page_size/num_layers/num_kv_heads/head_dim 与三个派生 stride——全是 `usize` 值。这就是「判据是构造了什么」的锚点。

**② 两个纯 CPU 测试：只碰 KvLayout。** [src/kv_pool.rs:473-493](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L473-L493)：`stride_geometry_qwen35` 与 `stride_geometry_qwen3` 用真实模型几何（8 层/4 头/256 维 与 36 层/4 头/128 维，page_size=16）断言 `kv_block_len`/`layer_stride`/`page_stride` 的手算值。它们不构造 `KvPool`，所以不碰 GPU。

**③ GPU 测试的显式红灯。** [src/kv_pool.rs:448-456](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L448-L456)：`mod tests` 的公共 helper `test_pool` 第一行就是 `DeviceContext::new().expect("GPU required for kv_pool tests")`，随后用最小几何（1 层/1 头/dim=1）建池——注释写明「just enough to exercise logic」。`storage_width_sets_backing_capacity`（[src/kv_pool.rs:458-471](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L458-L471)）、`kv_state_lifecycle`、`kv_state_out_of_pages`、`kv_state_drop_returns_pages` 四个测试都经此 helper 进入 GPU 层。

**④ bail 路径的现有覆盖。** [src/kv_pool.rs:540-548](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L540-L548)：`kv_state_out_of_pages` 用 3 页的池（1 页 padding + 2 页可用）先 `ensure_capacity(32)` 恰好占满、再断言 `ensure_capacity(33).is_err()`——这正好覆盖了 [src/kv_pool.rs:360-374](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L360-L374) 里 `try_grow` 失败后的 `bail!("KvState: out of pages …")`。对照可见**未覆盖**的 bail/分支：构造器的两处溢出 bail（见 4.2.3）、`desc_for_len` 的 `ensure!` 校验（[src/kv_pool.rs:397-406](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L397-L406)）、以及 4.3 要处理的 `absorb`。

**⑤ 纯 CPU 测试样板：page_pool。** [src/page_pool.rs:132-194](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L132-L194)：三个测试全部只构造 `PagePool`（`Mutex<Vec<PageId>>`，无任何设备对象）。`acquires_pages_and_restores_on_drop`（[src/page_pool.rs:137-163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L137-L163)）验证「取出→drop→free-list 完整恢复且页号升序复现」；`try_grow_extends_permit_and_returns_all_on_drop`（[src/page_pool.rs:173-193](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L173-L193)）同时覆盖 grow 成功与「grow 超余量失败后 permit 不变」两个分支——这就是「一个测试断言一个分支语义」的样板。

**⑥ 唯一的集成测试。** [tests/weight_loader_gpu.rs:1-11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/tests/weight_loader_gpu.rs#L1-L11)：头注释一行定调「GPU, model-free: StagedWeightLoader state-machine contract.」，import 用的是 `use pegainfer_core::tensor::DeviceContext` 与 `use pegainfer_core::weight_loader::StagedWeightLoader`——**外部 crate 路径**。[tests/weight_loader_gpu.rs:12-37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/tests/weight_loader_gpu.rs#L12-L37) 的 `record_after_finish_is_rejected` 在内存里现场序列化一个 2×2 的 BF16 safetensors 分片，走「记录 → finish → 再记录必须被拒 → take 赎回」的完整状态机；[tests/weight_loader_gpu.rs:39-77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/tests/weight_loader_gpu.rs#L39-L77) 的 `unaligned_vector_payload_roundtrips` 用「在相邻两个偏移上摆放归档、保留 payload 指针为奇数的那份」的技巧强制走非对齐 BF16 的 owned-decode 路径，再 H2D/D2H round-trip 逐位校验。

#### 4.1.4 代码实践

**实践目标**：亲眼确认三层测试的边界与 GPU 红灯的设计意图。

**操作步骤**：

1. 在仓库根目录运行 `cargo test --release -p pegainfer-core --lib 2>&1 | tail -5`，记录通过/失败数。
2. 运行 `cargo test --release -p pegainfer-core --lib page_pool -- --nocapture`，确认 3 个 page_pool 测试全绿。
3. 运行 `cargo test --release -p pegainfer-core --lib kv_state -- --nocapture`。
4. 运行 `cargo test --release -p pegainfer-core --test weight_loader_gpu 2>&1 | tail -5`。

**需要观察的现象**：步骤 3 在有 GPU 的机器上应看到 3 个 `kv_state_*` 测试通过；在无 GPU 的机器上应以 `panicked at 'GPU required for kv_pool tests'` 风格的消息显式失败（消息来自 [src/kv_pool.rs:453](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L453) 的 `expect`）。步骤 4 与 `--lib` 互不影响——`--lib` 根本不编译 `tests/` 目录。

**预期结果**：无 GPU 环境下，步骤 1/2 可过、步骤 3/4 显式红灯；这正是「命令分层 + 运行时红灯」而非 `#[ignore]` 的隔离法。本讲义在无 GPU 沙盒中撰写，以上均为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `stride_geometry_qwen3` 不需要 GPU，而 `storage_width_sets_backing_capacity` 需要？

答案：前者只调用 `KvLayout::new`（[src/kv_pool.rs:473-493](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L473-L493)），`KvLayout` 是文档自证的纯值类型（[src/kv_pool.rs:15-17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L15-L17)）；后者构造两个不同 `KvStorage` 宽度的 `KvPool`（[src/kv_pool.rs:458-471](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L458-L471)），池构造要 `alloc_zeros` 分配 `CudaSlice<bf16>`，必须有 CUDA 设备。

**练习 2**：用 `grep -rn 'ignore' src/ tests/` 验证「core 不用 `#[ignore]`」。

答案：命中只有三处——`src/weight_loader.rs:297` 的文档代码块标记 ```ignore、以及 `eq_ignore_ascii_case` 两处字符串比较用法；没有任何测试属性 `#[ignore]`。GPU 隔离由命令分层（`--lib` vs 按名/`--test`）与运行时 `expect` 完成。

**练习 3**：如果把 `tests/weight_loader_gpu.rs` 的两个测试搬进 `src/weight_loader.rs` 的 `mod tests`，会失去什么？

答案：失去外部 crate 视角——集成测试只能 `use pegainfer_core::…` 触到 `pub` API，恰好复现模型 crate 消费者的契约（u3-l3 讲过的「记录→finish→take」三段状态机是跨 crate 承诺）；同时 `cargo test --lib` 快车道会被迫连带编译并运行这两个 GPU 测试，纯 CPU 快速迭代被打断。

### 4.2 精度门与环境变量：core 之上的验证闸口

#### 4.2.1 概念说明

core 的 43+2 个测试守护的是**结构与资源不变量**：页守恒、stride 手算对拍、状态机合法迁移。它们不回答「模型输出对不对」。数值正确性的证据链在模型 crate 层闭合：

- **`hf_golden_gate`（精度门）**：CLAUDE.md 明确「Qwen accuracy gates compare logits against stored HF golden fixtures」——把前向 logits 与存档的 HuggingFace 基准对拍。core 里任何影响数值路径的改动（哪怕只是改一个 stride 推导）最终都要在这里过闸。
- **`e2e_scheduler`（调度活性与请求流）**：Qwen3.5 的 exact-text JSON 基线已退役，保留它覆盖调度器活性与请求流转。

环境变量也分两层。**core 自己只读两个**：`RUST_LOG`（[src/logging.rs:167](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/logging.rs#L167)）与 `PEGAINFER_TRACE_OTLP_ENDPOINT`（[src/tracing.rs:33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/tracing.rs#L33) 常量定义、[src/tracing.rs:52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/tracing.rs#L52) 读取，未设置则静默跳过 OTLP）。CLAUDE.md 里的大部分 `PEGAINFER_*`（如 `PEGAINFER_TEST_MODEL_PATH`）属于模型 crate 或构建系统，与 core 无关——写测试时别把层级搞混。

#### 4.2.2 核心流程

一次 core 改动的证据链（与 CLAUDE.md 的 AI 辅助贡献要求对齐）：

```text
改 core
 ├─ ① 结构不变量 → cargo test --lib（快车道，43 个）
 ├─ ② 涉设备路径 → 按名跑 GPU 单测 / --test weight_loader_gpu
 ├─ ③ 影响前向数值 → 模型 crate 精度门（hf_golden_gate）
 │      PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test --release \
 │        -p pegainfer-qwen35 --features qwen35 --test e2e_scheduler
 ├─ ④ 性能类改动 → same-context A/B（同上下文对比，CLAUDE.md 要求）
 └─ ⑤ 输出/精度变化 → 模型 eval
```

关键点：**层级只能向上补，不能向下省**。改了 `KvLayout` 的 stride 推导，①的手算对拍会告诉你几何没变，但只有③能证明 logits 没变。

#### 4.2.3 源码精读

**① 构造器的 bail 面：core 的错误输出也是契约。** [src/kv_pool.rs:186-239](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L186-L239)：`with_storage_and_format` 是所有构造路径的汇点（`new`/`with_storage` 都汇聚到这里），含两处未被测试覆盖的溢出 bail——[src/kv_pool.rs:204-209](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L204-L209) 在元素域做 `checked_mul`，[src/kv_pool.rs:212-218](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L212-L218) 在字节域再做一次（注释说明「分配器对字节数的乘法是不检查的，先在这里负责」），失败返回 `anyhow!("KvPool geometry overflows …")`。这是「core 改动的验证层级」的典型样本：溢出分支理论上纯 CPU 可判，但函数签名要 `&DeviceContext`，所以测试只能进 GPU 层。同一函数里的 [src/kv_pool.rs:226-229](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L226-L229) 用 `try_acquire_many(1).expect("pool must have at least 1 page for padding")` 在构造期预留 padding 页——u5-l1 讲过的确定性纪律就落在这两行。

**② 精度门的命令与文件。** CLAUDE.md（[仓库根目录](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md)，本讲撰写沙盒只授权读 `pegainfer-core/` 目录，行号待确认）给出权威命令：

```bash
cargo test --release -p pegainfer-qwen3 --test hf_golden_gate
PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test --release \
  -p pegainfer-qwen35 --features qwen35 --test e2e_scheduler
```

对应文件分别是 [pegainfer-qwen3/tests/hf_golden_gate.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/tests/hf_golden_gate.rs) 与 [pegainfer-qwen35/tests/e2e_scheduler.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen35/tests/e2e_scheduler.rs)（由 cargo `--test <name>` 对应 `tests/<name>.rs` 的关系推得，行号待确认）。

**③ core 侧的环境变量消费点。** [src/tracing.rs:18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/tracing.rs#L18) 的文档示例与 [src/tracing.rs:52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/tracing.rs#L52) 的 `let Ok(endpoint) = std::env::var(OTLP_ENDPOINT_ENV) else` 说明：不设置就整段跳过，core 测试不依赖任何环境变量即可跑。

#### 4.2.4 代码实践

**实践目标**：为假想改动规划证据链，训练「层级选择」。

**操作步骤**（纸面练习 + 可选上机）：

1. 假想改动：把 [src/kv_pool.rs:360-374](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L360-L374) 的 `ensure_capacity` 从「一次 grow 到足量」改成「逐页循环 grow」。页数公式不变：\(\text{pages}=\lceil \text{tokens}/\text{page\_size}\rceil\)。
2. 填一张证据链表：哪条既有测试守护它（提示：`kv_state_out_of_pages` 覆盖失败语义、`kv_state_lifecycle` 覆盖跨页记账），需要新增什么测试，是否需要上 ③ 精度门。
3. （可选，需 GPU）上机验证第 2 步结论：跑 4.1.4 的命令。

**需要观察的现象 / 预期结果**：逐页循环不改变「needed ≤ held 即返回、grow 失败即 bail」的对外语义，①+② 层应全部保持绿色且无需新增精度门；但如果改动了页获取顺序（影响 `page_indices_i32` 的页序），就必须补 ③。纸面结论待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PEGAINFER_TEST_MODEL_PATH` 不该出现在 core 的测试里？

答案：它是模型 crate 精度门/集成测试的模型权重路径覆盖（CLAUDE.md 的测试章节），core 的 43+2 个测试都不加载模型权重——`tests/weight_loader_gpu.rs` 甚至在内存里现场造 safetensors 归档。core 读的环境变量只有 `RUST_LOG` 与 `PEGAINFER_TRACE_OTLP_ENDPOINT`。

**练习 2**：改了 `src/rope.rs` 的频率表生成（u3-l4 讲过的 theta 指数），最低要过到第几层？

答案：至少到 ③。RoPE 表直接进入前向数值路径，`--lib` 的 rope 单测只能验证表结构与既有断言，logits 是否漂移必须由 `hf_golden_gate` 对拍；若精度变化是预期的，按 CLAUDE.md 还要补模型 eval。

### 4.3 贡献与提交规范：从分支到 Commitizen

#### 4.3.1 概念说明

CLAUDE.md 的 Git Conventions 一节（行号待确认，见上文说明）定了四条硬规矩：

1. **提交信息用 Commitizen 格式** `<type>(<scope>): <subject>`。
2. **禁止直推 `main`**——先建 `feat/`、`fix/`、`chore/` 等分支。
3. **提交前 `cargo fmt`**——CI 跑 `cargo fmt --check`，不过就挂。
4. **AI 辅助 PR 必须可问责、可验证**：「Show that the work is not duplicated, name the production invariant being changed, and provide real evidence: production E2E for features and fixes, same-context A/B for performance, and model evals for output or accuracy changes. Every changed file must serve that invariant; remove unrelated cleanup, thin wrappers, generated scaffolding, and mock or source-text tests presented as production evidence.」

第 4 条的两个关键词值得展开：

- **name the production invariant**：PR 要能说出自己改的是哪条生产不变量（本手册 u5-l1 讲的两段式准入、页守恒、padding 确定性都是现成的不变量词汇表）。
- **mock 或源码文本测试不是生产证据**：断言「源码里包含某字符串」、用假对象替代真实 GPU 路径的测试，都不算证明。仓库历史里有直接先例：`dbd73337 refactor(gemma4): the tests call the production path instead of shaping it (#947)`——专门花一次提交把测试改成「调用生产路径而不是塑形它」。

真实的 Commitizen 样本（当前 HEAD 的 `git log` 实测）：

```text
fix(kimi-k2): select distinct router experts when choices are NaN (#1170)
feat(qwen3): add native DFlash2 checkpoint and selector (#1064)
perf(deepseek-v2-lite): run steady decode attention on device (#1162)
chore(project): rename OpenInfer to PegaInfer (#841)
```

顺带一提仓库的卫生文化：[src/page_pool.rs:83-87](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L83-L87) 留着一条审计注释——「`is_empty` was dead code (hawk sweep, #743); `len` stands alone.」死代码会被清扫，新增测试时不要顺手添加无人使用的小工具函数；另外 [Cargo.toml:38-39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/Cargo.toml#L38-L39) 的 `[lints] workspace = true` 说明 lint 阈值统一在工作区根部管理，别在 crate 里自行放宽。

#### 4.3.2 核心流程

一次完整贡献的七步：

```text
1. git switch -c test/core-page-pool-absorb-coverage   # 分支，不直推 main
2. 写改动 + 就地补「生产性」单测（调用生产路径，不 mock）
3. cargo fmt                                            # CI 会跑 --check
4. cargo test --release -p pegainfer-core --lib          # 层级①
5. （涉 GPU/契约）按 4.1/4.2 补层级②③
6. git commit -m 'test(core): <subject>'                 # Commitizen
7. PR 描述：点名不变量 + 证据（E2E / A/B / eval）+ 每个文件为何在场
```

#### 4.3.3 源码精读

**① 演练靶点：`absorb` 没有任何测试。** [src/page_pool.rs:89-97](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L89-L97)：`OwnedPagePermit::absorb` 先用 `Arc::ptr_eq` 断言两个 permit 来自同一个池（否则以 `"absorb requires permits from the same pool"` panic），再把 `other.pages` 整体搬进自己。全 crate 检索它的使用点只有一个——[src/kv_pool.rs:380-384](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L380-L384) 的 `commit_reservation`（注释写明这是两段式准入的「apply 半段」），而 u5-l1 已经把这条链上升为纪律——纪律的实现函数却零测试覆盖。同时段的 `try_grow` 有专门测试（[src/page_pool.rs:173-193](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L173-L193)），两相对照，缺口一目了然。

**② absorb 的语义细节。** [src/page_pool.rs:120-130](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L120-L130)：`Drop for OwnedPagePermit` 在 `pages` 为空时直接返回——`absorb` 用 `Vec::append` 搬空 `other.pages` 后，被吸收的 permit 析构时走的就是这个早退分支，**不会**把同一批页归还两次。这正是单测要钉死的语义。

**③ 零预约分支。** [src/page_pool.rs:45-51](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L45-L51)：`try_acquire_many(0)` 返回空 permit；生产代码 [src/kv_pool.rs:242-248](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L242-L248) 的 `KvPool::alloc` 就靠它给新请求发「零页起点」。这个分支同样没有直接测试（`kv_state_lifecycle` 只是间接经过）。

#### 4.3.4 代码实践

**实践目标**：为 `absorb` 补一个生产性单测（层级①，纯 CPU 快车道），并完成提交演练。

**操作步骤**：

1. 建分支：`git switch -c test/core-page-pool-absorb-coverage`。
2. 在 [src/page_pool.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs) 的 `mod tests`（L132 起）内追加以下**示例代码**（风格对齐现有三个测试）：

```rust
// 示例代码：吸收后页随吸收方统一归还，且空 permit 析构不重复归还
#[test]
fn absorb_merges_permits_and_returns_all_on_drop() {
    let pool = PagePool::new(4);
    let mut a = pool.try_acquire_many(1).expect("acquire a");
    let b = pool.try_acquire_many(2).expect("acquire b");
    assert_eq!(pool.available_pages(), 1);

    a.absorb(b); // b 被搬空；其 Drop 走空页早退分支
    assert_eq!(a.len(), 3);
    assert_eq!(pool.available_pages(), 1);

    drop(a);
    assert_eq!(pool.available_pages(), 4); // 3 页随吸收方一次性归还
}

// 示例代码：跨池吸收必须 panic（页会还错 free list）
#[test]
#[should_panic(expected = "absorb requires permits from the same pool")]
fn absorb_rejects_permits_from_another_pool() {
    let pool_a = PagePool::new(1);
    let pool_b = PagePool::new(1);
    let mut a = pool_a.try_acquire_many(1).expect("acquire from a");
    let b = pool_b.try_acquire_many(1).expect("acquire from b");
    a.absorb(b);
}
```

3. 跑 `cargo test --release -p pegainfer-core --lib page_pool -- --nocapture`，应见 **5 passed**（原 3 + 新 2）。
4. `cargo fmt`，然后写提交信息（演练，**不实际提交**）：

```text
test(core): cover OwnedPagePermit::absorb merge and cross-pool guard

The merge is the apply half of the two-phase KV admission
(kv_pool::commit_reservation) and had no direct coverage. Pin the
drop-conservation semantics and the same-pool guard. No behavior change.
```

**需要观察的现象**：新测试不依赖 GPU（`PagePool` 无设备对象），随 `--lib` 快车道运行；`should_panic` 的 expected 子串与 [src/page_pool.rs:94](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L94) 的断言消息一致。

**预期结果**：5 passed；提交信息自检通过「每个改动文件服务于同一不变量」——单文件、单不变量（free-list 守恒）。撰写沙盒无编译环境，运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：以下哪几项在本仓库会被视为「非生产证据」？(a) 断言 `kv_pool.rs` 源码里含有 "out of pages" 字符串的测试；(b) `kv_state_out_of_pages`；(c) 一个用假 DeviceContext 绕过 CUDA 的 absorb 测试。

答案：(a)、(c) 是。(a) 是 CLAUDE.md 点名禁止的「source-text tests presented as production evidence」——它证明不了运行时行为；(c) 用 mock 替代真实路径，测到的不是生产语义。(b) 是生产证据——它调用真实的 `KvPool::alloc`/`ensure_capacity` 并在真实失败路径上断言 `is_err()`（[src/kv_pool.rs:540-548](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L540-L548)）。

**练习 2**：为一个把 decode 每步耗时降低 8% 的 core 改动写 Commitizen 提交信息与证据要求。

答案：`perf(core): <subject>`（参照真实样本 `perf(deepseek-v2-lite): run steady decode attention on device (#1162)`）；证据必须是与改动前**同上下文**的 A/B 对比（CLAUDE.md：same-context A/B for performance），并在 PR 里点名被改变的生产不变量。

**练习 3**：为什么演练的分支名以 `test/` 开头而不是 `feat/`？

答案：CLAUDE.md 要求「create a `feat/`/`fix/`/`chore/`/… branch first」——列表以「…」开放，惯例是分支前缀与提交类型一致；纯测试补充不改行为，`test(core):` 提交配 `test/…` 分支最自洽。

## 5. 综合实践

**演练一次完整贡献（不实际提交）**，把 4.1–4.3 串起来：

1. **读唯一的集成测试**：通读 [tests/weight_loader_gpu.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/tests/weight_loader_gpu.rs)（77 行），写三行归纳：(a) 它测 `StagedWeightLoader` 的哪段状态机契约；(b) 为什么必须独占 `tests/` 文件——外部 crate 视角 + GPU 需求 + `--lib` 不编译 `tests/`；(c) 它如何在无模型权重的前提下构造输入（内存现场序列化 safetensors，含奇偶性技巧）。
2. **挑一个未被覆盖的分支**：候选清单（本讲实测）——`page_pool::absorb`（推荐，纯 CPU）、`try_acquire_many(0)` 零预约分支、kv_pool 构造器溢出 bail（[src/kv_pool.rs:204-218](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L204-L218)）、`desc_for_len` 的 `ensure!`（[src/kv_pool.rs:397-406](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L397-L406)）。按 4.1.2 的判据先判定各属于哪个验证层级，再动手。
3. **写测试并跑通**：风格对齐被测文件现有的 `mod tests`；`cargo fmt`；按层级跑命令。进阶（需 GPU）：在 kv_pool 的 `mod tests` 里补两段式准入的回滚测试——`try_reserve(1)` 后让 `KvReservation` 直接 drop，断言页回到池；再 `commit_reservation` 过户，断言 `held_pages` 增加（页数记账参照 [src/kv_pool.rs:495-538](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L495-L538) 的「N 页总量 − 1 页 padding」注释法）。
4. **写分支名与 Commitizen 提交信息**（模板见 4.3.4 第 4 步）。
5. **自查「每个改动文件服务于同一不变量」**：用一句话写出你的不变量（如「页所有权只存在于 permit，跨 permit 转移后 free-list 守恒」），检查 diff 里没有任何与它无关的清理、包装或脚手架；不满足就拆分或删除。

## 6. 本讲小结

- core 的测试分三层：43 个内嵌单测（12 个文件，其中 kv_pool 的 2 个 stride 测试纯 CPU、4 个需 GPU）+ `tests/weight_loader_gpu.rs` 的 2 个 GPU 集成测试 + 模型 crate 的精度门；分层判据是「改动构造了什么」。
- GPU 隔离不靠 `#[ignore]`（全 crate 无此属性），靠命令分层与 `DeviceContext::new().expect("GPU required …")` 的运行时显式红灯。
- 证据链只能向上补不能向下省：结构不变量在 ①② 层闭合，数值正确性必须到 `hf_golden_gate` / `e2e_scheduler` 过闸；core 自己只读 `RUST_LOG` 与 `PEGAINFER_TRACE_OTLP_ENDPOINT`。
- 贡献四规矩：Commitizen、不直推 main、提交前 `cargo fmt`、AI 辅助 PR 必须点名不变量并给出真证据；mock 与源码文本测试被明令禁止（仓库先例 #947「tests call the production path」）。
- 实测的覆盖缺口：`absorb`/`try_reserve`/`commit_reservation`（两段式准入的 apply 半段）、构造器溢出 bail、`desc_for_len` 校验——它们是第一次真实 PR 的最佳起点。

## 7. 下一步学习建议

手册到此收官。建议的后续路径：

1. **回读 u5-l1**，把本讲的「不变量词汇表」（两段式准入、RAII 守恒、padding 确定性）与它的反例调度序列对照，你会发现自己已经能读懂每条纪律对应的测试义务。
2. **去模型 crate 跑一次精度门**：按 CLAUDE.md 的命令跑 `hf_golden_gate`，读断言怎么对拍 golden logits——那是 core 改动证据链的终点站。
3. **读 CLAUDE.md 的 Team Documentation Workflow 与 `docs/models/glm52/free-running-dp.md`**：贡献不只是代码，文档同步（TL;DR 契约、index.md 路由）同样是仓库纪律。
4. **用 4.3 的七步流程提第一个真实 PR**：从第 5 节清单里挑一个缺口开始，`test/` 分支 + Commitizen + 不变量自述。
