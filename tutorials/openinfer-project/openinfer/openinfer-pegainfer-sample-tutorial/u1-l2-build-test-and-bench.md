# 构建、测试与基准：验证 pegainfer-sample 的三条途径

## 1. 本讲目标

上一讲（u1-l1）我们看清了 `pegainfer-sample` 在整个推理引擎里的分层位置：它是一个只拥有「策略」的小 crate，底下的 `.cu`/FFI 与批式原语都在 `pegainfer-kernels`。本讲回答一个工程上同等重要的问题：**这个 crate 怎么验证？**

读完本讲，你应该能够：

1. 区分这个 crate 的三类验证面：lib 内嵌的 CPU 单元测试、需要 GPU 的集成测试、需要 GPU 的基准与延迟探针。
2. 独立运行 `cargo test --release -p pegainfer-sample --lib`，并**从源码出发**解释它为什么在运行时不需要 GPU。
3. 说出被 `#[ignore]` 的延迟探针的用途，以及手动运行它的完整命令。
4. 理解 `benches/select_batch.rs` 中 greedy / sampling / mixed 三种参数混合各自代表什么服务形态、分别测量什么。
5. 理解 workspace 的 `--release` 纪律与 `[lints] workspace = true` 对你写测试代码的实际约束。

## 2. 前置知识

承接 u1-l1 的两个核心概念：**logits arena**（一个 `rows × vocab` 的 bf16 设备矩阵，每行是一个 token 的完整打分）和 **采样策略层**（本 crate 的 `select_batch` 把 arena 变成每行一个 token id）。忘记的读者请先回看 u1-l1。

本讲还需要几个 Cargo 与测试的基础概念，用通俗语言先解释清楚：

- **crate 与 target**：一个 Rust 包（crate）可以包含多个编译目标（target）。`src/lib.rs` 是库目标（lib target）；`tests/` 目录下每个 `.rs` 文件会被编译成一个独立的**集成测试**二进制（它只能使用 crate 的公共 API，像外部使用者一样）；`benches/` 下的文件是**基准**目标。
- **`--lib` 与不加 `--lib` 的区别**：`cargo test -p pegainfer-sample --lib` 只运行库目标内部 `#[cfg(test)] mod tests` 里的单元测试；`cargo test -p pegainfer-sample` 则会把 lib 测试和 `tests/` 下所有集成测试二进制都跑一遍。
- **criterion**：Rust 生态最常用的微基准框架。它反复执行被测函数、做统计抽样，报告每次调用的平均耗时与置信区间，适合回答「这段代码有多快」；而集成测试回答「这段代码对不对」。
- **`#[ignore]`**：Rust 测试属性。被标记的测试默认跳过，必须用 `cargo test -- --ignored` 显式要求才会运行。常用于「很慢」或「不是正确性门」的测试。
- **`--release` 纪律**：本仓库的约定（见仓库 CLAUDE.md）是**永远用 `--release`**——debug 构建对 GPU/CUDA 代码极慢，会超时。即使跑纯 CPU 的单元测试，也保持同样习惯，让行为与测量和真实部署一致。

一个会在本讲反复出现的小数学事实（详细论证留给 u2-l1）：softmax 之后概率最大的那个 token，其概率 \( \max_i p_i \ge 1/V \)（\( V \) 是词表大小），因为 \( V \) 个和为 1 的数里最大者不可能小于平均值。这解释了为什么 `top_p <= 1/V` 的核里只剩 argmax 一个 token——两个集成测试文件里各有一个回归测试就冻结在这条论证上。

## 3. 本讲源码地图

| 文件 | 作用 | 是否本讲主角 |
| --- | --- | --- |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml#L1-L24) | 本 crate 的依赖表、dev-dependencies、workspace lints 接入、显式 bench 声明 | 是 |
| [Cargo.toml（workspace 根）](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L1-L27) | 成员列表、`default-members`、criterion 版本、workspace lints 全表 | 是（只读两段） |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L651-L704) | 约 700 行的全部库代码；本讲只精读文件末尾 `#[cfg(test)] mod tests` 的三个 CPU 单元测试 | 是 |
| [tests/select_batch.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L1-L431) | 9 个需要 GPU 的路由行为测试 + 1 个 `#[ignore]` 的延迟探针 | 是 |
| [tests/gpu_logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L1-L337) | 7 个需要 GPU 的「设备-主机 A/B」logprob 测试 | 是 |
| [benches/select_batch.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L1-L78) | criterion 微基准：vocab 151936 × batch {1,8,32,64} × 三种参数混合 | 是 |

## 4. 核心概念与源码讲解

### 4.1 workspace 构建、--release 纪律与 lints

#### 4.1.1 概念说明

`pegainfer-sample` 是一个 **cargo workspace 的成员 crate**，不是独立项目。这带来三个直接后果：

1. **必须用 `-p` 指定它**：workspace 根的 `Cargo.toml` 把 `default-members` 设成了只有 `pegainfer-server`，在仓库根裸跑 `cargo build` 不会碰这个 crate。
2. **构建会连带编译 CUDA 依赖链**：本 crate 依赖 `pegainfer-kernels`，而后者用自己的 `build.rs` 调 nvcc 编译 `.cu`。所以「编译这个 crate」需要 CUDA 工具链；但「运行它的三个单元测试」不需要 GPU 设备——这是两件事，后面 4.2 会精确区分。
3. **lint 由 workspace 统一治理**：`[lints] workspace = true` 让本 crate（包括测试代码）继承根上一整张 clippy lint 表，你在这个 crate 里新增测试时同样受它约束。

#### 4.1.2 核心流程

```
cargo test --release -p pegainfer-sample --lib
  │
  ├─ 解析 workspace（根 Cargo.toml：members 含 pegainfer-sample）
  ├─ 构建依赖：anyhow / half / cudarc（crates.io）
  │            + pegainfer-frontend（契约类型，无 CUDA）
  │            + pegainfer-kernels（build.rs：nvcc 编译 .cu → FFI）   ← 构建耗时大头
  ├─ 编译 pegainfer-sample 的 lib 目标（含 #[cfg(test)] mod tests）
  └─ 运行 3 个纯主机算术测试（不构造 DeviceContext，不触碰任何 CUDA 入口）
```

若构建机器没有 GPU 设备（`nvidia-smi` 不可用），需要用环境变量 `PEGAINFER_CUDA_SM` 显式指定编译目标（例如 `PEGAINFER_CUDA_SM=120`），这是仓库 CLAUDE.md 记录的官方做法；nvcc 本身仍然必须存在。

#### 4.1.3 源码精读

本 crate 的依赖表非常短——四个运行时依赖加一个 dev 依赖：

[Cargo.toml:L8-L16](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml#L8-L16) —— 运行时依赖是 `anyhow`（错误处理）、`cudarc`（CUDA driver 绑定）、`half`（bf16 类型）、`pegainfer-frontend`（`SamplingParams`/`TokenLogprob` 契约）与 `pegainfer-kernels`（底层算子）；dev-dependencies 只有 `criterion`，即基准只在测试/基准编译时引入，不进正常构建。

[Cargo.toml:L18-L23](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml#L18-L23) —— 三行关键配置：`[lints] workspace = true` 接入 workspace lint 表；`autobenches = false` 关闭基准的**自动发现**；随后显式声明唯一的 `[[bench]]`，名字 `select_batch`、`harness = false`。`harness = false` 表示不用 cargo 默认的 libtest 测试框架，而是让 criterion 自己提供 `main`（通过文件末尾的 `criterion_main!`）。两者合起来的效果：`cargo bench -p pegainfer-sample` 只会运行这一个声明的 criterion 基准。

在 workspace 根侧，只看两段：

[Cargo.toml（workspace 根）:L2-L20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L2-L20) —— `default-members = ["pegainfer-server"]` 意味着在根目录裸跑 `cargo build`/`cargo test` 只作用于 server；`pegainfer-sample` 在 `members` 列表第 20 行，必须 `-p pegainfer-sample` 点名。

[Cargo.toml（workspace 根）:L199-L235](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L199-L235) —— workspace lint 表：`clippy::pedantic` 整组开成 `warn`，再放行一批噪音项；额外挑了几个 nursery/restriction 级别的 lint（例如 `dbg_macro = "warn"`、`exit = "warn"`、`redundant_clone = "warn"`）；Rust 侧还有 `unreachable_pub` 与 `unsafe_op_in_unsafe_fn`。对你的直接影响：测试代码里不能写 `dbg!`（用 `eprintln!`，探针正是这么做的），并且 `cargo fmt --check` 在 CI 里把关格式——给本 crate 添加任何测试后都要跑一次 `cargo fmt`。

#### 4.1.4 代码实践

**实践：构建面体检（无需 GPU 设备，需 CUDA 工具链；实际运行结果待本地验证）**

1. **实践目标**：亲手确认「编译这个 crate」与「运行三个单元测试」分别需要什么。
2. **操作步骤**：
   - 打开 workspace 根 `Cargo.toml`，找到 `default-members` 与 `members` 里的 `pegainfer-sample`，理解为什么必须 `-p`。
   - 构建库目标：`cargo build --release -p pegainfer-sample`。若机器无 GPU 设备，先 `export PEGAINFER_CUDA_SM=120`（或你目标架构的 SM 值）再构建。
   - 只列出不运行：`cargo test --release -p pegainfer-sample --lib -- --list`。
   - 运行：`cargo test --release -p pegainfer-sample --lib`。
3. **需要观察的现象**：构建耗时几乎全部花在 `pegainfer-kernels`（nvcc 编译）上；`-- --list` 应恰好列出 3 个测试名。
4. **预期结果**：3 个测试全部通过，名字与 4.2.3 精读的三个一致：`token_logprob_matches_exact_log_softmax`、`token_logprob_k_larger_than_vocab`、`token_logprob_f32_input_and_guards`。本讲义撰写环境未实际执行该命令，运行输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：在仓库根直接跑 `cargo build`，会编译到 `pegainfer-sample` 吗？为什么？
**答案**：不会。根 `Cargo.toml` 把 `default-members` 设为 `["pegainfer-server"]`，裸命令只作用于默认成员；要点名这个 crate 必须写 `cargo build --release -p pegainfer-sample`。

**练习 2**：`autobenches = false` 加显式 `[[bench]] name = "select_batch" harness = false` 这个组合的净效果是什么？
**答案**：cargo 不再自动把 `benches/` 下发现的文件注册为基准，只有被显式声明、且用自己的 `main`（criterion）取代 libtest harness 的 `select_batch` 基准会被 `cargo bench -p pegainfer-sample` 运行——基准面完全可枚举、无意外。

**练习 3**：`[lints] workspace = true` 对你要往本 crate 加测试有什么现实约束？举两个。
**答案**：任选两个即可，例如 (1) `clippy::dbg_macro = "warn"`——调试输出要用 `eprintln!` 而不是 `dbg!`；(2) 整组 `clippy::pedantic` 在 warn 级别生效，测试代码同样要过 pedantic；此外仓库 CI 跑 `cargo fmt --check`，新测试需先 `cargo fmt`。

### 4.2 CPU 单元测试与 GPU 门测试的分界

#### 4.2.1 概念说明

这个 crate 的 19 个测试（3 + 9 + 7，外加 1 个被 ignore 的探针）分属两个世界：

- **CPU 世界**：`src/lib.rs` 末尾 `#[cfg(test)] mod tests` 里的 3 个单元测试。它们只调用主机函数 `token_logprob_from_row`（纯算术：一遍 O(V) 求最大值与秩、f64 累加指数和、维护 top-k），整个测试模块没有 `use` 任何 kernels 类型，更没有构造 `DeviceContext`。所以**运行时**不需要 GPU。
- **GPU 世界**：`tests/` 下两个集成测试文件的全部测试。每一个的第一行有效语句都是 `DeviceContext::new().unwrap()`——初始化 CUDA 设备上下文。这就是「GPU 门」：没有 GPU 的机器上它们会在这一行直接失败。

判定规则一句话：**读源码，看测试是否构造 `DeviceContext`（或向 `HiddenStates` 上传设备数据）**。有，就需要 GPU；没有，就是纯主机测试。

还有一个容易混淆的细节：`tests/select_batch.rs` 里有一个测试叫 `batch_larger_than_scratch_is_rejected`，它断言的其实是 `select_batch` 顶部 `ensure!` 的主机侧容量校验失败——但它的前置步骤仍要构造 `DeviceContext` 和设备上的 arena/scratch，所以它照样被 GPU 门拦住。「需要 GPU 才能跑」与「测试的是 GPU 行为」是两回事。

最后看两个 GPU 文件的**分工哲学**（各自的模块文档写得很清楚）：

- `tests/select_batch.rs` 验证**门与路由**：本 crate 在裸核之上加的策略——贪心/非贪心路由、token 归位、scratch 复用、容量不变量。原始核正确性（top-k/top-p/温度、philox 决定性）归 `pegainfer-kernels` 所有，这里不重测。
- `tests/gpu_logprobs.rs` 验证**双实现互证**：设备端 `token_logprobs_batch` 必须与主机参考实现 `token_logprob_from_row` 在同一批 bf16 行上给出一致结果（top-k 的 id 完全相等，数值在容差内）——这是经典的「GPU-主机 A/B」正确性门。

#### 4.2.2 核心流程

`cargo test --release -p pegainfer-sample`（不加 `--lib`）会编译并运行三个测试二进制：

```
cargo test --release -p pegainfer-sample
 ├─ lib 目标（src/lib.rs 内嵌）      → 3 个 CPU 测试        （无 GPU 也能跑）
 ├─ 集成目标 select_batch             → 9 个 GPU 测试        （DeviceContext::new() 处被 GPU 门拦截）
 │                                    + 1 个 #[ignore] 探针  （默认跳过，见 4.3）
 └─ 集成目标 gpu_logprobs             → 7 个 GPU 测试        （同上）
```

CPU 测试与 GPU A/B 测试的容差也体现了两个世界的差异：单元测试用 bf16 可精确表示的输入，期望值是解析算出的，容差收到 `1e-6`；GPU A/B 两边都是 f32 浮点、累加顺序不同，容差放宽到 `TOL = 5e-5`。

#### 4.2.3 源码精读

**CPU 侧：三个单元测试。**

[src/lib.rs:L651-L656](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L651-L656) —— `#[cfg(test)] mod tests` 的导入只有两样：`half::bf16` 与 `super::token_logprob_from_row`。没有任何 kernels、任何设备类型——这就是「不需要 GPU」的直接源码证据。

[src/lib.rs:L657-L678](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L657-L678) —— 第一个测试 `token_logprob_matches_exact_log_softmax`：输入 `[1.0, 3.0, 2.0, 0.0, 3.0]`（全部 bf16 精确），在 f64 里解析算出对数配分函数 \[ \mathrm{LSE} = \ln\!\big(e^{1}+e^{3}+e^{2}+1+e^{3}\big) \] 再断言 picked 的 logprob 满足 \( |\,\text{logprob} - (2.0-\mathrm{LSE})\,| < 10^{-6} \)；同时断言 rank（用原始 logit 比较得到的名次）与 top-3 的 id 序列 `[1, 4, 2]`——两个并列的 3.0 按更小的 token id 在前。

[src/lib.rs:L680-L693](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L680-L693) —— 第二个测试 `token_logprob_k_larger_than_vocab`：`top_k = 32` 大于词表长度 2 时被截断为 2；并验证 log-softmax 的概率和为 1（\( \sum_i e^{\text{logprob}_i} = 1 \)，容差 1e-6）。

[src/lib.rs:L695-L703](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L695-L703) —— 第三个测试 `token_logprob_f32_input_and_guards`：覆盖泛型的 f32 路径（Qwen 的用法）与两个守卫——空行返回 `None`、picked 越界返回 `None`。

**GPU 侧：两个集成测试文件的门与手法。**

[tests/select_batch.rs:L1-L8](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L1-L8) —— 模块文档开宗明义：合成 logits arena、不加载模型权重；「核正确性归 kernels，这里检查的是门把每行接进正确路径、token 落在正确下标」；最后一行明确写着 "Requires a GPU"。

[tests/select_batch.rs:L20-L31](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L20-L31) 与 [L51-L55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L51-L55) —— 两个关键辅助函数：`make_arena` 把若干行 f32 转成 bf16 后上传成设备 arena（`HiddenStates::zeros` + `memcpy_htod` + `ctx.sync()`），`peak_row` 造「平地上一个尖峰」的单行 logits。这两个函数是全文件**模型无关测试法**的基石——不需要任何模型权重就能驱动完整采样路径。

[tests/select_batch.rs:L57-L59](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L57-L59) —— 第一个 GPU 测试的开头，`DeviceContext::new().unwrap()` 就是 GPU 门本身；文件里 9 个活动测试全部以这一行开始。

[tests/gpu_logprobs.rs:L1-L13](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L1-L13) —— GPU-主机 A/B 的契约：top-k 的 id 必须**逐个相等**，数值容差 `TOL = 5e-5`。

[tests/gpu_logprobs.rs:L37-L55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L37-L55) —— `assert_matches_host` 把设备输出与主机参考实现逐项对照（rank 相等、logprob/top-k 数值在容差内、top-k id 序列相等），7 个测试全部复用这一个断言器。

[tests/gpu_logprobs.rs:L246-L260](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L246-L260) —— 一个值得注意的测试手法：测试把 `inflated.seq_len` 手动改成 4（大于 arena 实际 backing 的 1 行），断言 `token_logprobs_batch` 仍会拒绝——证明校验读的是元数据字段而非依赖 allocator 撞运，越界拒绝是显式逻辑。

#### 4.2.4 代码实践

**实践：给全部测试画一张 GPU 依赖分界表（无需 GPU）**

1. **实践目标**：不看运行结果、只读源码，判定每个测试/基准是否需要 GPU，并给出依据。
2. **操作步骤**：
   - 在 `pegainfer-sample` 目录下用搜索工具分别查 `DeviceContext::new` 在 `src/lib.rs`、`tests/select_batch.rs`、`tests/gpu_logprobs.rs`、`benches/select_batch.rs` 中的出现位置。
   - 为每个文件的每个测试建一行记录：测试名 / 是否构造 `DeviceContext` / 是否需要 GPU。
   - 特判两处：`tests/select_batch.rs` 末尾带 `#[ignore]` 的探针；`batch_larger_than_scratch_is_rejected`（注意它测的是主机侧 `ensure!` 但仍被 GPU 门拦截，说明理由）。
3. **需要观察的现象**：`src/lib.rs` 的测试模块零命中；三个 GPU 文件的每个测试函数体内第一行附近各有一次命中。
4. **预期结果****（可直接核对）**：

| 位置 | 数量 | 需要 GPU？ | 依据 |
| --- | --- | --- | --- |
| `src/lib.rs` 内嵌单元测试 | 3 | 否（运行时） | 测试模块无任何设备类型，只调 `token_logprob_from_row` |
| `tests/select_batch.rs` 活动测试 | 9 | 是 | 每个开头 `DeviceContext::new().unwrap()` |
| `tests/select_batch.rs` 延迟探针 | 1（`#[ignore]`） | 是（且需手动运行） | 同上，见 4.3 |
| `tests/gpu_logprobs.rs` 测试 | 7 | 是 | 同上，其中一个还激活 `StreamOverrideGuard` |
| `benches/select_batch.rs` | 1 个基准（3 组混合 × 4 个 batch 档） | 是 | `bench_select_batch` 开头构造 `DeviceContext` |

5. 加分项：`cargo test --release -p pegainfer-sample --lib -- --list` 的输出应与你表中的 lib 部分一致（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`cargo test --release -p pegainfer-sample`（不加 `--lib`）在一台没有 GPU 的机器上会发生什么？
**答案**：先编译三个测试目标；lib 目标的 3 个测试通过；`select_batch` 与 `gpu_logprobs` 两个集成二进制里每个测试都在第一行 `DeviceContext::new().unwrap()` 处 panic（unwrap 到 Err），全部失败。所以无 GPU 机器上正确的命令是加 `--lib`。

**练习 2**：单元测试的容差是 `1e-6`，GPU A/B 的容差是 `5e-5`，为什么差 50 倍？
**答案**：单元测试的输入 `[1.0, 3.0, 2.0, 0.0, 3.0]` 等都能被 bf16 精确表示，期望值用 f64 解析算出，只检验实现与数学的偏差，可以收紧到 1e-6；GPU A/B 两边（设备核与主机参考）都是 f32 计算、且 15 万宽词表上的累加顺序不同，浮点舍入路径天然不同，容差必须放宽到 5e-5。

**练习 3**：`tests/select_batch.rs` 的模块文档把哪些正确性划给 `pegainfer-kernels`、哪些留给本 crate？这样划分的好处是什么？
**答案**：top-k/top-p/温度/philox 决定性等**裸核正确性**归 kernels；本 crate 只测**策略**——贪心/非贪心路由、token 归位、scratch 复用、容量拒绝。好处是测试不重复、失败定位快：路由测试挂了问题在 sample 层，核测试挂了问题在 kernels 层。

### 4.3 criterion 微基准与延迟探针

#### 4.3.1 概念说明

正确性之外，这个 crate 还有两件「测速」工具，回答的问题不同：

- **criterion 微基准**（`benches/select_batch.rs`）：统计式地测量 `select_batch` 每次调用的耗时，覆盖 vocab 151936（Qwen3 词表）× batch {1, 8, 32, 64} × 三种参数混合。三种混合不是随便挑的：
  - **greedy**（temperature=0）——所有行走批式 indexed argmax。这是**批式 argmax 的成本下限**：不含任何 FlashInfer 采样通道，量出「路由 + argmax + 回读」的最小开销。
  - **sampling**（t=1.0, top_k=50, top_p=0.9）——所有行走 FlashInfer 批式采样，量采样通道的全额成本。
  - **mixed**——贪心与采样行交替，即**常见服务形态**：真实在线流量里温度为 0 的请求和带采样参数的请求混在同一个 decode 批里，`select_batch` 必须一次调用同时服务两路。
- **延迟探针**（`select_batch_span17_latency_probe`，被 `#[ignore]`）：来自具体工程问题（#512：验证轮采样器成本）的一次性测量——span-17 行、Qwen 词表、三种参数，热身后计时 200 次取平均，把「每次调用多少微秒」用 `eprintln!` 打到 stderr。它**不是正确性门**，所以默认不跑，避免拖慢每次 `cargo test`；需要时手动 `--ignored` 运行。它与 criterion 的分工：criterion 面向回归跟踪（多轮抽样、报告置信区间、可与上次对比），探针面向「快速看一个数字、回答一个具体问题」。

#### 4.3.2 核心流程

criterion 基准的一次运行：

```
cargo bench -p pegainfer-sample
  → 编译 benches/select_batch.rs（bench profile，继承 release 优化）
  → bench_select_batch:
      构造 DeviceContext（GPU 门）
      vocab = 151_936
      for batch in [1, 8, 32, 64]:
          造确定性非平坦 logits arena
          for mix in [greedy, sampling, mixed]:
              criterion 以统计抽样反复调用 select_batch
              （每次迭代 seed += 1，忠实引擎「每解码步推进种子」的契约）
  → 报告形如 select_batch/greedy/32、select_batch/mixed/64 的每组均值与置信区间
```

注意 `cargo bench` 本身就使用 bench profile（默认继承 release 的优化级别），不必也不应加 `--release`。

#### 4.3.3 源码精读

[benches/select_batch.rs:L1-L3](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L1-L3) —— 模块文档一句话点明三组混合的语义："Greedy is the batched argmax floor; sampling is the FlashInfer pass; mixed is the common serving case. Requires a GPU."

[benches/select_batch.rs:L16-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L16-L25) —— `arena` 用整数哈希（`(i * 2_654_435_761) % 1000 * 0.01`）生成**确定性非平坦** logits——平坦全零的 arena 会让 top-k/top-p 空转，测出的采样耗时失真。

[benches/select_batch.rs:L42-L60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L42-L60) —— 驱动面：batch 四档 `[1usize, 8, 32, 64]`；`greedy` 是 `p(0.0, -1, 1.0)`（temperature 0 → `is_greedy` → 全体走 argmax），`sampling` 是 `p(1.0, 50, 0.9)`，`mixed` 按行号奇偶交替构造两种参数。

[benches/select_batch.rs:L63-L70](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L63-L70) —— 迭代闭包里 `seed = seed.wrapping_add(1)` 后再调 `select_batch`：`select_batch` 的契约要求种子每解码步新鲜（无种子行靠 philox 子序列去相关），基准保持同样的节奏，而不是让每次迭代重复同一棵随机树。`BenchmarkId::new(name, batch)` 生成 `select_batch/<mix>/<batch>` 形式的报告分组。

[benches/select_batch.rs:L76-L77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L76-L77) —— `criterion_group!`/`criterion_main!` 提供基准二进制自己的入口，这正是 Cargo.toml 里 `harness = false` 的对应物。

[tests/select_batch.rs:L373-L378](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L373-L378) —— 探针的身份注解：注释写明动机（#512 验证轮采样器成本、span-17 行、Qwen 词表、greedy 对比采样路径、「不是正确性门——打印每次调用微秒数，用 --nocapture 运行」），`#[ignore = "GPU latency probe — run manually with --nocapture"]` 让它默认缺席。

[tests/select_batch.rs:L416-L428](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L416-L428) —— 测量主体：每组参数先空转 20 次热身并 `ctx.sync()`，再对 200 次计时（同样以 sync 收尾），最后 `eprintln!` 输出 `select_batch span17 <case>: <us> us/call`。三组 case 分别是 greedy、`t0.8_topp0.95`、`t0.7_topk20_topp0.9`（定义在同函数上文 [L392-L411](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L392-L411)）。

#### 4.3.4 代码实践

**实践：读基准、写命令、预测报告（预测部分无需 GPU）**

1. **实践目标**：会跑这个 crate 的基准与探针，并能**在运行前**说出会得到哪些报告条目。
2. **操作步骤**：
   - 通读 `benches/select_batch.rs`，写出 `cargo bench -p pegainfer-sample` 将产生的全部 benchmark id（提示：3 种混合 × 4 个 batch 档位）。
   - 写出探针的完整运行命令（要求：只跑探针这个测试、包含被 ignore 的测试、并让 stderr 打印可见）。
   - （有 GPU 的机器，可选）实际运行两条命令；再试单跑一个分组：`cargo bench -p pegainfer-sample --bench select_batch -- mixed`。
   - （有 GPU 的机器，可选）把 batch 档位列表复制一份加上 `128`，对比 `select_batch/greedy/64` 与 `select_batch/greedy/128` 的每调用耗时是否近似线性增长。改动前先 `git diff` 确认只动了这一行，跑完 `git checkout -- benches/select_batch.rs` 还原。
3. **需要观察的现象**：同一 batch 档位下 `greedy < mixed < sampling` 的耗时排序是否成立；criterion 是否在终端输出均值与置信区间（workspace 开了 criterion 的 `html_reports` 特性，还会在 `target/criterion/report/` 生成 HTML 报告）。
4. **预期结果**：
   - benchmark id 共 12 个：`select_batch/{greedy,sampling,mixed}/{1,8,32,64}`。
   - 探针命令：`cargo test --release -p pegainfer-sample --test select_batch -- --ignored --nocapture`（该文件只有这一个 ignored 测试，故不必再按名过滤；如需精确过滤可加测试名 `span17`）。
   - 无 GPU 环境下，本实践的第 1、2 步（纯阅读与写命令）即可完成；第 3、4 步的实测数字**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 greedy 档测的是「批式 argmax 的下限」？
**答案**：greedy 参数（temperature=0）使 `is_greedy()` 成立，批内所有行都路由进同一次批式 indexed argmax，FlashInfer 采样通道完全不启动——量到的只有路由判定、argmax 核与 D2H 回读的固定成本，是 `select_batch` 任何一次调用的成本地板。

**练习 2**：探针为什么要 `#[ignore]`，而不是删掉或并入 criterion？
**答案**：它不是正确性门，只是一次性的延迟测量（#512 的遗留问答），跑起来要数百次全词表调用且需要 GPU；放进默认 `cargo test` 会拖慢所有例行验证。保留成 ignored 测试让它留在最贴近被测代码的地方，需要时一条命令即可复现测量。

**练习 3**：基准迭代里为什么每次 `seed += 1`，而不是固定一个种子？
**答案**：`select_batch` 的契约是引擎种子每解码步推进、无种子行靠 philox 子序列去相关；固定种子会让每次迭代重复完全相同的随机数消费路径，测出的「采样」可能退化成重复同一棵树，既不真实也可能让耗时失真。

## 5. 综合实践

**任务：为 pegainfer-sample 制作一张完整的「验证面地图」并核对公共 API 文档。**

把本讲三条途径串成一份可保存的笔记（建议直接放进你自己的学习目录）：

1. **构建与 CPU 测试**：运行 `cargo test --release -p pegainfer-sample --lib`（无 GPU 设备时先设 `PEGAINFER_CUDA_SM`），记录 3 个测试名与结果；再运行 `cargo test --release -p pegainfer-sample --lib -- --list` 截存输出。
2. **GPU 分界表**：完成 4.2.4 的分界表（3 + 9 + 1 ignored + 7 个测试与 1 个基准），每行附判定依据（是否构造 `DeviceContext`）。
3. **命令速查卡**：用自己的话写下四条命令分别做什么——`--lib` 单元测试、不加 `--lib` 的全量测试（需 GPU）、`-- --ignored --nocapture` 探针、`cargo bench -p pegainfer-sample` 基准——并注明各自需要的硬件。
4. **文档核对**：运行 `cargo doc --no-deps -p pegainfer-sample`，打开 `target/doc/pegainfer_sample/index.html`，把页面列出的公共项与 u1-l1 的 API 清单逐一核对。应能找到：函数 `select_batch`、`token_logprob_from_row`、`token_logprobs_batch`、`mix_seed`、`effectively_greedy`，以及 `greedy_argmax_ids` / `greedy_stage_readback` / `greedy_stage_resident` / `greedy_collect_resident` 四件套；结构体 `SampleScratch`（含 `new`/`with_selection_width`/`max_rows`/`vocab`）与 `LogprobRequest`；四条再导出 `SamplingParams`、`BatchSamplingRow`、`BatchSamplingScratch`、`gpu_sample_batch_into`。同时注意 `TokenLogprob` 只是被私有 `use` 的契约类型（出现在返回值签名里、链接到 `pegainfer_frontend` 文档），**不在**本 crate 的再导出清单中——这是一个容易记错的点。
5. 交付物：一页 Markdown（表格 + 命令 + 文档核对结论）。全程不需要 GPU；第 1 步的测试输出**待本地验证**。

## 6. 本讲小结

- 这个 crate 的验证面分三类：lib 内嵌的 **3 个 CPU 单元测试**（只测 `token_logprob_from_row` 的主机算术）、`tests/` 下 **16 个 GPU 集成测试**（9 个路由行为 + 7 个 GPU-主机 A/B）、以及 **criterion 微基准 + 1 个 `#[ignore]` 延迟探针**。
- 判定一个测试是否需要 GPU 的可靠方法不是运行它，而是看它是否构造 `DeviceContext`；`cargo test --release -p pegainfer-sample --lib` 在运行时不触碰任何 CUDA 入口，但**构建**仍需 CUDA 工具链（无 GPU 设备时用 `PEGAINFER_CUDA_SM` 指定目标）。
- workspace 纪律三件事：根 `Cargo.toml` 的 `default-members` 要求用 `-p pegainfer-sample` 点名；`--release` 是全仓库铁律；`[lints] workspace = true` 让 pedantic clippy 与 `cargo fmt --check` 同样约束测试代码。
- 基准三组混合各有含义：greedy 是批式 argmax 的成本下限、sampling 是 FlashInfer 通道的全额成本、mixed 是真实服务里两路共批的常见形态；每次迭代推进种子以忠实每步新鲜种子的契约。
- 探针 `select_batch_span17_latency_probe` 是 #512 的遗留问答：不是正确性门、默认 ignored，用 `-- --ignored --nocapture` 手动运行，打印每调用微秒数。
- 两个集成文件体现两种测试哲学：`select_batch.rs` 用合成 arena 做**模型无关**的路由测试（核正确性归 kernels），`gpu_logprobs.rs` 用**双实现互证**（设备 vs 主机参考，容差 5e-5）做数值正确性门。

## 7. 下一步学习建议

本讲解决了「怎么验证」，下一讲（u2-l1）进入第一个真机制：`SamplingParams` 各字段的语义与 `effectively_greedy` 判定——为什么 `top_p <= 1/vocab` 的行必须走确定性的 argmax 路径，否则拒绝采样器会在 bf16 并列的最大值里随机挑一个。届时本讲提到的两个回归测试（`tiny_top_p_routes_to_argmax_even_under_bf16_ties` 与 `padded_arena_width_does_not_suppress_the_argmax_routing`）会成为数学论证的冻结形式，建议先把这两个测试的注释通读一遍再去读 u2-l1。

如果想在下一讲之前热身源码，推荐两条短线：

- 读 `src/lib.rs` 中 `token_logprob_from_row`（[L473-L520](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L473-L520)）——本讲三个 CPU 测试的被测函数，也是 u3-l1 的主角。
- 对照 `benches/select_batch.rs` 与 `select_batch` 的文档注释（[L162-L188](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L162-L188)），尝试只从文档猜出三路分发结构，为 u2-l3 做铺垫。
