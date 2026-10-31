# 引擎契约接入：SimulatedEngineConfig 与 start_engine

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `SimulatedEngineConfig::new(...)` 加一串 `with_*` builder 方法，组装出一份"定时参数 + 假想行为"俱全的模拟引擎配置，并说清每个旋钮控制什么。
2. 解释 `start_engine` / `start_engine_with_partitions` 如何把一份配置克隆成 N 个调度器线程，以及 `partitions` 与 `Engine.schedulers.len()` 的严格对应关系。
3. 说出 `EngineInfo` 中 `kv_capacity: None`、`servable_len: None` 对 vLLM 前端意味着什么（跳过哪些检查、保留什么默认值），以及为什么"显式的 None"是一种契约表态而不是疏忽。

本讲是单元一的收尾：u1-l1 让你知道这个 crate 是什么，u1-l2 讲了进程怎么启动，本讲补上中间那一层——**配置对象如何被装配成前端真正持有的 `Engine`**。

## 2. 前置知识

### 2.1 消费型 builder 模式

`SimulatedEngineConfig` 的 `with_*` 方法都长这样：

```rust
#[must_use]
pub fn with_fallback_token_id(mut self, fallback_token_id: u32) -> Self {
    self.fallback_token_id = fallback_token_id;
    self
}
```

它拿走 `self`（按值）、改一个字段、再把 `self` 还回来，因此可以链式调用：

```rust
let config = SimulatedEngineConfig::default()
    .with_fallback_token_id(1)
    .with_engine_profile(profile.clone())?;
```

这叫**消费型 builder**（consuming builder）。`#[must_use]` 让你忘记接住返回值时编译器直接警告。与 Java 风格的 `setXxx()`（原地修改、无返回值）不同，链中途丢掉结果就等于什么都没改。

### 2.2 `Option` 是"行为开关"，不只是"可能没有"

这个 crate 里 `Option` 有两种用法，含义不同：

- `Option<(usize, usize)>`（如 `spec_decode`、`prefix_cache`）：`None` 表示**该行为不存在**。没有配前缀缓存，就不会上报任何前缀缓存计数器——`None` 不是"值为零"，而是"这件事根本没发生"。
- `Option<u32>`（如 `EngineInfo::servable_len`）：`None` 表示**显式声明"我不报告这个能力"**，前端据此选择跳过某类校验。

### 2.3 你需要记得的前情

- u1-l1 讲过：sim 有两条互斥车道——legacy 固定定时车道与 `--profile` 档案车道。本讲会看到这个分岔在**配置层**的样子（`profile: Option<ProfileConfig>` 字段）。
- u1-l2 讲过：legacy 模式 CLI 用 `SimulatedEngineConfig::new(...)` 组装，profile 模式用 `default().with_fallback_token_id(..).with_engine_profile(..)` 组装。本讲从引擎侧解释这两条组装路径。
- 前端契约：`pegainfer_frontend::engine` 定义了 `Engine`、`Scheduler` 等 trait/struct；模型侧 crate 只需产出满足契约的对象，HTTP 层就能把它当真引擎伺服。

### 2.4 OS 线程与 channel 的最小图景

`spawn_scheduler` 会为每个分区启动一个**操作系统线程**，线程里跑一个无休止的轮询循环；前端侧拿到的是一个 handle（内含提交请求的 channel 和接收 step 事件的 channel）。本讲只需要这个图景，循环细节在 4.4 展开。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `pegainfer-sim/src/lib.rs` | 本讲主角：`SimulatedEngineConfig` 定义、`start_engine*` 装配函数、`SimScheduler` 及其单测（含 `collect_completion` 辅助函数） |
| `pegainfer-sim/src/main.rs` | CLI 侧的消费者：两条组装路径的实例（本讲只回看组装那几行） |
| `pegainfer-frontend/src/engine/wiring.rs` | 契约定义：`Engine`、`LiveScheduler`、`EngineInfo` |
| `pegainfer-frontend/src/engine/driver.rs` | `Scheduler` trait、`spawn_scheduler` 与驱动循环 `drive` |
| `pegainfer-sim/tests/frontend_e2e.rs` | e2e 视角：`start_engine_with_partitions` 与 `serve_with_engine_count` 的配对使用、分区数不匹配的拒绝测试 |

## 4. 核心概念与源码讲解

### 4.1 SimulatedEngineConfig 及 with_* builder

#### 4.1.1 概念说明

`SimulatedEngineConfig` 是模拟引擎的"旋钮面板"。真实引擎的行为由模型权重和 GPU 决定；模拟引擎没有这两样，于是**所有行为都被抽成了配置项**：

- 三个定时参数决定"多快"（TTFT/TPOT 模型，u1-l2 已讲）；
- 其余字段决定"演什么"：要不要假想投机解码、要不要假装查了前缀缓存、输出哪些 token、空 prompt 用哪个 token 兜底、要不要切到档案车道。

因为每个 `SimScheduler` 持有一份完整的 `SimulatedEngineConfig`（见 4.4），这个结构体就是 sim 引擎全部可变行为的单一定义点。

#### 4.1.2 核心流程

组装一份配置的过程：

1. 入口二选一：
   - `SimulatedEngineConfig::new(base_ttft_ms, prefill_tokens_per_ms, tpot_ms, fallback_token_id)` —— 立即校验三个定时参数（有限、非负；吞吐必须严格为正），非法即返回 `Err`；
   - `SimulatedEngineConfig::default()` —— 直接给一套定时默认值（5.0 / 100.0 / 12.0，与 `main.rs` 里的 `DEFAULT_*` 常量一致）。
2. 按需链上 `with_*`：每个 `with_*` 打开或改写一种假想行为，全部 `#[must_use]`，除 `with_engine_profile` 外都不可能失败。
3. `with_engine_profile(profile)` 是唯一的 `Result` 返回 builder：它会先调 `profile.validate()?` 做全量校验，通过后才切到档案车道。

校验的数学条件（`new` 内的三个 `ensure!`）：

\[ \text{base\_ttft\_ms} \ge 0,\quad \text{prefill\_tokens\_per\_ms} > 0,\quad \text{tpot\_ms} \ge 0,\quad \text{三者均有限} \]

吞吐必须**严格为正**：TTFT 公式 \( \text{base} + \text{prompt\_len} / \text{throughput} \) 里它当分母，零或负都会让除法失去意义。

#### 4.1.3 源码精读

先看结构体全貌——九个字段就是九类旋钮：

> [pegainfer-sim/src/lib.rs:45-61](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L45-L61)
> 定义 `SimulatedEngineConfig`：三个定时参数、`fallback_token_id`、`scripted_completion`（非空时逐字重放为输出）、`spec_decode: Option<(K, accepted)>`（假想投机解码）、`profile: Option<ProfileConfig>`（档案车道开关）、`prefix_cache: Option<(本地命中, 外部恢复)>`（假想前缀缓存查询）。

构造函数与三重校验：

> [pegainfer-sim/src/lib.rs:69-98](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L69-L98)
> `new(...)` 用三个 `ensure!` 分别拒绝负数/NaN/无穷的定时值与非正的 prefill 吞吐，通过后以"空脚本、无 drafter、无 profile、无 prefix cache"的干净底座返回。

四个行为旋钮：

> [pegainfer-sim/src/lib.rs:103-115](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L103-L115)
> `with_speculative_decoding(K, accepted)`：先 `assert!` "验证步接受数不得超过提议数"，再借 `SpecDecodeCounters::new(K)` 提前确认 K 不越界（越界则 panic），把假想 drafter 存进配置。

> [pegainfer-sim/src/lib.rs:121-124](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L121-L124)
> `with_prefix_cache(hit_tokens, external_hit_tokens)`：声明"每个普通请求首个 chunk 上报一次前缀缓存命中"，命中 0 即模拟 miss。

> [pegainfer-sim/src/lib.rs:128-138](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L128-L138)
> `with_scripted_completion(ids)`：指定输出 token 序列逐字重放；`with_fallback_token_id(id)`：改写空 prompt 时使用的兜底 token（默认 0）。

> [pegainfer-sim/src/lib.rs:142-146](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L142-L146)
> `with_engine_profile(profile)`：唯一返回 `Result` 的 builder——装载前先 `profile.validate()?`，确保进入在线车道的是一份完整校验过的档案 bundle。

`Default` 实现给出的默认定时（可与 `main.rs` 的 `DEFAULT_*` 对照，数值完全一致）：

> [pegainfer-sim/src/lib.rs:157-170](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L157-L170)
> `Default` 提供 base TTFT 5ms、prefill 吞吐 100 token/ms、TPOT 12ms、fallback token 0，其余行为全关。

定时换算辅助（供 legacy 车道调用）：

> [pegainfer-sim/src/lib.rs:148-154](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L148-L154)
> `ttft(prompt_tokens)` 返回 \( \text{base} + \text{len}/\text{throughput} \) 换算出的 `Duration`，`tpot()` 返回固定间隔；两者都经由 `duration_from_ms` 再做一次有限性防御。

CLI 的两条组装路径正好是这个 builder 的两个用例（回顾 u1-l2）：

> [pegainfer-sim/src/main.rs:99-101](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L99-L101)
> 档案模式：`default()` 起底（定时值将被档案接管，所以不传定时参数），只链 `with_fallback_token_id` 与 `with_engine_profile`。

> [pegainfer-sim/src/main.rs:130-140](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L130-L140)
> legacy 模式：从 CLI 参数（带 `DEFAULT_*` 兜底）取出三个定时值，走 `new(...)` 的校验入口。

#### 4.1.4 代码实践

**实践目标**：亲手触发 `new` 的校验路径，确认非法定时值被挡在配置构造这一层。

1. 操作步骤：运行仓库里现成的校验测试，只跑这一条：
   ```bash
   cargo test --release -p pegainfer-sim --lib config_rejects_invalid_timing_values -- --nocapture
   ```
2. 需要观察的现象：测试通过。它的断言来自 [pegainfer-sim/src/lib.rs:1014-1022](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L1014-L1022)——负 TTFT、零吞吐、负 TPOT、NaN、两种无穷，六种输入全部 `is_err()`。
3. 预期结果：`test result: ok. 1 passed`。
4. 想再进一步：在你的本地工作副本里临时给该测试加一行 `assert!(SimulatedEngineConfig::new(5.0, f64::NAN, 12.0, 0).is_err());`，重跑确认同样通过（NaN 吞吐同样非法；`> 0.0` 对 NaN 判 false）。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`with_speculative_decoding(3, 5)` 会发生什么？为什么这个检查放在 builder 里而不是调度器运行时？

答案：builder 直接 panic——`assert!(num_accepted <= num_spec_tokens)` 不满足（5 > 3）。这是一个配置自洽性错误（"验证步接受数多于提议数"在物理上不可能），属于程序员笔误而非运行期故障，fail-fast 在配置构造期最便宜；相比之下调度器每步都查一遍是浪费。同函数里 `SpecDecodeCounters::new(K)` 的 `.expect("K within MAX_SPEC_TOKENS")` 也是同一逻辑：K 越界是配置错误，panic 优于带病运行。

**练习 2**：为什么 `with_engine_profile` 是唯一返回 `Result` 的 `with_*`，而其他都是直接返回 `Self`？

答案：其他旋钮只是往字段里写一个平凡值，不存在失败可能；而装载档案涉及解析与全量校验（`profile.validate()?`，涵盖 schema 版本、调度器约束、覆盖域与计时网格，u3 单元细讲），校验失败必须以 `Err` 而不是 panic 呈现——CLI 场景下用户传一个坏档案文件应当得到一条可读的报错（u1-l2 讲过的 `failed to load engine profile ...` 上下文），而不是进程崩溃。

**练习 3**：`Default::default()` 与 `new(5.0, 100.0, 12.0, 0)` 产生的配置有区别吗？

答案：没有实质区别——对照 [lib.rs:157-170](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L157-L160) 与 `new` 的字段初始化，两者的九个字段完全相同。区别只在路径：`new` 会跑校验（这组值合法，所以通过），`default` 直接构造。档案模式选 `default()` 起底，正是因为定时参数在该车道无意义，不该再走一遍定时校验。

### 4.2 start_engine / start_engine_with_partitions

#### 4.2.1 概念说明

有了配置，还差一步：把它变成前端能用的 `Engine`。这一步由两个函数完成：

- `start_engine(config)` —— 单分区便捷入口，内部转发到 `start_engine_with_partitions(config, 1)`；
- `start_engine_with_partitions(config, partitions)` —— 真正的装配函数。

`partitions` 的语义是**前端可见的引擎数**。vLLM 前端启动时会声明"我要伺服 N 个引擎"；如果启动出的 `Engine` 里 `schedulers.len()` 与声明的 N 不一致，前端直接拒绝启动。所以参数不叫 `threads` 或 `count` 而叫 `partitions`：它模拟的是真实引擎的并行度拓扑（如 TP/EP 分片），每个分片一个调度器线程。

#### 4.2.2 核心流程

```
start_engine_with_partitions(config, partitions)
  ├─ assert!(partitions > 0)                    // 引擎至少要暴露一个调度器
  ├─ Engine {
  │    schedulers: (0..partitions).map(|index|
  │        spawn_scheduler("pegainfer-sim-{index}",   // 每分区一个具名 OS 线程
  │                         SimScheduler::new(config.clone())))  // 配置逐份克隆
  │        .collect(),
  │    info: EngineInfo { kv_capacity: None, servable_len: None },  // 无 KV、无服务上限
  │    lora: None,                              // 不伺服 LoRA
  │  }
```

要点：配置是**按值克隆**给每个调度器的——各分区互不共享可变状态，`SimScheduler` 拥有自己的队列、计数器与（档案车道下的）`WorkerState`。

#### 4.2.3 源码精读

> [pegainfer-sim/src/lib.rs:172-176](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L172-L176)
> `start_engine`：文档注释点名 `partitions` 是"前端可见的引擎数（声明 N 个引擎的测试必须起 N 个调度器）"，实现只是转发 `1`。

> [pegainfer-sim/src/lib.rs:178-198](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L178-L198)
> `start_engine_with_partitions`：断言 `partitions > 0` 后，用 `(0..partitions).map(...)` 为每个分区 `spawn_scheduler`（线程名 `pegainfer-sim-{index}`，调度器由 `SimScheduler::new(config.clone())` 现场构造），并装配 `EngineInfo { kv_capacity: None, servable_len: None }` 与 `lora: None`。

单测对"分区数 = 调度器数"的直接锁定：

> [pegainfer-sim/src/lib.rs:1042-1046](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L1042-L1046)
> `start_engine_with_partitions_exposes_that_many_schedulers`：起 3 个分区，断言 `engine.schedulers.len() == 3`。

e2e 侧的配对用法与不匹配时的拒绝：

> [pegainfer-sim/tests/frontend_e2e.rs:208-221](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L208-L221)
> e2e 辅助函数 `spawn_once`：`start_engine_with_partitions(&config, engine_count)` 起出的调度器数，必须与随后 `serve_with_engine_count(..., engine_count, ...)` 声明的引擎数是同一个值。

> [pegainfer-sim/tests/frontend_e2e.rs:1096-1123](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L1096-L1123)
> `frontend_rejects_engine_partition_mismatch`：`start_engine` 只起 1 个调度器，却向 `serve_with_engine_count` 声明 2——断言 serve 返回 `Err`，错误信息含 "declared 2 engines but the launched engine exposes 1 schedulers"。这就是"partitions 必须如实"的反面教材。

main.rs 的调用点（u1-l2 已精读，这里只定位）：

> [pegainfer-sim/src/main.rs:213](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/main.rs#L213)
> 二进制入口用单分区 `start_engine(&runtime.engine)` 起引擎，随后交给 `vllm::serve`（隐式声明 1 个引擎）。

#### 4.2.4 代码实践

**实践目标**：用一条命令验证"分区数即调度器数"，并看懂一处刻意的失败。

1. 操作步骤：
   ```bash
   cargo test --release -p pegainfer-sim --lib start_engine_with_partitions -- --nocapture
   cargo test --release -p pegainfer-sim --test frontend_e2e frontend_rejects_engine_partition_mismatch -- --nocapture
   ```
2. 需要观察的现象：第一条测试通过（3 分区 → 3 调度器）；第二条测试**也**通过——它断言的是 serve 启动失败并带着那句 mismatch 错误信息，"通过"的含义是"前端正确地拒绝了错误配置"。
3. 预期结果：两条均 `ok`。注意第二条是 `#[tokio::test]`，需要编译 e2e 测试目标，耗时明显长于 `--lib`。
4. 若想观察 `assert!(partitions > 0)` 的效果，可在本地工作副本临时调用 `start_engine_with_partitions(&config, 0)` 并 `cargo test --release -p pegainfer-sim --lib`，预期 panic 于 "an engine must expose at least one scheduler"。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `partitions == 0` 用 `assert!`（panic）而不是返回 `Result`？

答案：这是一个调用方编码错误（没有任何正常场景需要零调度器的引擎），不是用户输入错误。Rust 惯例是用 panic 暴露编程错误（类似数组越界），用 `Result` 表达可恢复的运行期故障。对比 4.1 的 `with_engine_profile`：坏档案文件来自用户输入，所以走 `Result`。

**练习 2**：e2e 里 `spawn_once` 为什么必须把 `engine_count` 同时传给 `start_engine_with_partitions` 和 `serve_with_engine_count`，而不是让一边推导另一边？

答案：两个函数各自只知道自己那一半真相——装配函数知道实际起了几个调度器，serve 只知道前端声明要伺服几个引擎。把两个数字解耦，`frontend_rejects_engine_partition_mismatch` 才能构造出"声明 2、实际 1"的错配来验证前端的启动期校验；生产代码里两边传同一个变量，即保证一致。

### 4.3 Engine / EngineInfo 装配

#### 4.3.1 概念说明

`Engine` 是前端契约里的"交接包"：模型侧 crate 产出它，协议栈（vLLM 兼容层）在装配期拆解它。`EngineInfo` 则是引擎对自身能力的**显式声明**，只有两个字段，但每个 `None` 都是有语义的：

- `kv_capacity: None` —— "我不报告 KV 池容量"，前端随之**跳过 batch-fit 检查**（不再按 KV 余量裁剪批量）；
- `servable_len: None` —— "我不报告最长可服务长度"，前端的 max-length 校验**退回到模型上下文窗口**（即 serve 时传入的 `max_model_len`）。

sim 引擎没有 KV cache（根本没有 GPU），两个都填 `None` 恰如其分：它想模拟的正是"一个不设 KV 门槛的引擎"。

#### 4.3.2 核心流程

```
Engine {
    schedulers: Vec<LiveScheduler>,   // 每分区一个：handle（提交/收流）+ join（驱动线程句柄）
    info:      EngineInfo { kv_capacity, servable_len },
    lora:      Option<LoraClient>,    // None = 无 LoRA 能力，没有独立开关与之矛盾
}
```

前端消费 info 的两条分支：

- `info.servable_len` 为 `Some(cap)` 时，生效上限 = `min(max_model_len, cap)`；为 `None` 时只用 `max_model_len`；
- `kv_capacity` 为 `None` 时，桥接层不报告 attention block size 等派生量，也不做 KV 适配。

#### 4.3.3 源码精读

> [pegainfer-frontend/src/engine/wiring.rs:145-153](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/wiring.rs#L145-L153)
> 契约定义 `Engine`：`schedulers` 的文档说明"多少个、各是什么含义是模型线自己的事"，`lora` 的 `Option` 即能力本身。

> [pegainfer-frontend/src/engine/wiring.rs:155-160](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/wiring.rs#L155-L160)
> `LiveScheduler`：`handle`（前端的提交与 step 流端点）加 `join`（驱动线程句柄，关停时等 handle 全部 drop 后由服务器 join）。

> [pegainfer-frontend/src/engine/wiring.rs:141-144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/wiring.rs#L141-L144)
> `Engine` 的文档注释把必填字段称作"入职清单"：一个不报告容量或服务上限的引擎必须**用 `None` 明说**，不能装作忘了。

> [pegainfer-frontend/src/engine/wiring.rs:182-190](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/wiring.rs#L182-L190)
> `EngineInfo` 两个 `Option` 字段的文档分别写明：无 KV 报告则前端跳过 batch-fit 检查；无 servable 长度则 max-length 校验落在模型上下文窗口。

前端实际消费 `servable_len` 的地方（`map` 出 `Some` 分支、`None` 落回 `max_model_len`）：

> [pegainfer-frontend/src/vllm/mod.rs:346](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/vllm/mod.rs#L346)
> `info.servable_len.map(|cap| max_model_len.min(cap))`：`None` 时整个表达式就是 `None`，生效上限退回 `max_model_len`。

`kv_capacity` 的 `None` 分支在桥接层的体现：

> [pegainfer-frontend/src/vllm/bridge.rs:832](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/vllm/bridge.rs#L832)
> `effective_attention_block_size: kv_capacity.map(...)`：无 KV 报告时不向 vLLM 协议层暴露派生的 block size（sim 场景下即为 `None`）。

sim 侧的装配（回看 4.2 引用的同一段代码的 info 部分）：

> [pegainfer-sim/src/lib.rs:192-196](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L192-L196)
> sim 填 `kv_capacity: None, servable_len: None`——诚实地声明"我没有 KV 池，也不设服务上限"。

#### 4.3.4 代码实践

**实践目标**：通过阅读对比，体会 `EngineInfo` 的 `None` 语义是"能力声明"而非默认值。

1. 操作步骤：打开上面引用的 [wiring.rs:182-190](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/wiring.rs#L182-L190) 与 [mod.rs:346](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/vllm/mod.rs#L346)，用 `Grep` 在 `pegainfer-frontend/src` 里搜 `servable_len`，逐个查看命中处对 `Some`/`None` 两分支的处理（本讲引用的两处即可入门）。
2. 需要观察的现象：`None` 从不表示"零"或"未初始化"——每处消费都伴随跳过某项检查或退回另一个上限的行为。
3. 预期结果：能口头回答"如果 sim 把 `servable_len` 错填成 `Some(8)` 会怎样"——前端会把生效上限压到 `min(max_model_len, 8)`，所有超过 8 token 的请求被拒，且这种错误不会在 sim 编译期暴露，只能靠行为测试发现。
4. 本实践为源码阅读型，无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：sim 的 `EngineInfo` 为什么不填 `lora: Some(...)` 哪怕是一个假的 LoRA client？

答案：`Engine` 的文档（[wiring.rs:150-152](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/wiring.rs#L150-L152)）写明 `Option` 就是能力本身，没有独立开关可以与之矛盾。sim 不模拟 LoRA 伺服，`None` 是唯一正确的声明；伪造一个 client 意味着前端会把 LoRA 相关请求路由过来，而 sim 无法兑现。

**练习 2**：`kv_capacity: None` 会让前端的 batch-fit 检查消失。对一个用于**基准测试**的模拟引擎，这是缺陷还是特性？

答案：是特性。sim 的用途是在 CPU 上压测前端协议栈与调度行为；KV 容量裁剪是真实引擎的资源约束，模拟它反而会把变量混进前端本身的测量。跳过检查让 sim 在任意批量下都"来者不拒"，使基准结果只反映前端与模拟定时的行为。（当然，若你想模拟 KV 受限引擎，就需要换一种诚实的建模方式——这正是档案车道 `SchedulerProfile` 里 `max_num_seqs`/`max_num_batched_tokens` 存在的原因之一，u3 展开。）

### 4.4 spawn_scheduler 每分区一线程

#### 4.4.1 概念说明

`spawn_scheduler` 是前端提供的通用工厂：给一个名字和一个实现了 `Scheduler` trait 的对象，它就：

1. 铸造配线（一对 channel：提交请求进、step 事件出，外加共享的 metrics 单元）；
2. 启动一个**具名 OS 线程**跑驱动循环 `drive`；
3. 把前端端的 `LiveScheduler { handle, join }` 还给你。

sim 的 `SimScheduler` 实现了 `Scheduler` trait 的三个方法：`submit`（收下请求）、`step`（推进一步）、`metrics`（快照指标）。这个 trait 是"循环留在前端、行为留在模型线"分工的关键：**循环只写一次**，所有模型线共享同一套约定（drain 顺序、每迭代一次 metrics 发布与一次 commit、致命错误处理）。

#### 4.4.2 核心流程

每个分区线程内的 `drive` 循环（一迭代）：

```
loop {
    排空 submissions channel      // 每个信封经 ledger.register 变成 QueuedRequest，交给 scheduler.submit
    step = scheduler.step(&mut ledger)   // 引擎侧唯一的判定写回点
    snapshot = scheduler.metrics()       // 指标快照
    metrics.publish(snapshot); ledger.commit_step(...)  // 每迭代恰好一次发布 + 一次提交
    if step 出错 {                        // 引擎不可救药
        ledger.fail_all(error)           // 给所有未结请求记账写销，带着真实错误
        return                           // 线程退出 → 整个引擎下线
    }
    if 空转（无 running 无 waiting 且 channel 已断） { 退出 }
    if 空转（还有 channel） { spin_loop 提示 }          // 不睡、不阻塞
}
```

`SimScheduler::new(config)` 在 spawn 前完成两件装备：若 `config.spec_decode` 有值则构造 `SpecDecodeCounters`；若 `config.profile` 有值则构造 `ProfiledRuntime`（档案车道的全部状态）。之后每次 `step` 按"有没有 `profiled`"在两条车道间分岔。

#### 4.4.3 源码精读

契约 trait——模型线的全部义务就这三个方法：

> [pegainfer-frontend/src/engine/driver.rs:20-41](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/driver.rs#L20-L41)
> `Scheduler` trait：`submit` 只做所有权转移（准许/拒绝的判定统一在 `step` 里写 ledger）；`step` 推进一步，可恢复失败由调度器自己消化，`Err` 表示引擎不可救药；`metrics` 出快照。文档强调循环运行在 `spawn_scheduler` 起的专用线程上，`Send` 即可。

工厂函数：

> [pegainfer-frontend/src/engine/driver.rs:45-52](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/driver.rs#L45-L52)
> `spawn_scheduler(name, scheduler)`：`scheduler_pair()` 铸配线，`std::thread::Builder::name(name).spawn(drive)` 起线程（线程名会出现在 `top`/gdb 里——这就是 sim 里 `pegainfer-sim-{index}` 的去向），返回 `LiveScheduler`。

驱动循环本体：

> [pegainfer-frontend/src/engine/driver.rs:59-105](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/driver.rs#L59-L105)
> `drive`：内层循环排空提交 channel（`register` → `submit`），然后 `step` → `metrics` → `publish` + `commit_step`；`step` 返回 `Err` 时 `ledger.fail_all` 把错误写销给每个未结账户后退出；完全空闲且 channel 已断则退出，否则 `spin_loop` 放松流水线再探测。

sim 侧的调度器构造：

> [pegainfer-sim/src/lib.rs:591-605](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L591-L605)
> `SimScheduler::new(config)`：克隆自配置的 `spec_decode` 换成真计数器（`.expect("K checked at config time")`——builder 期已查过）；`config.profile` 有值则构造 `ProfiledRuntime`（内含 `WorkerState`）；初始队列为空、前缀缓存计数清零。

trait 实现与车道分岔：

> [pegainfer-sim/src/lib.rs:619-641](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L619-L641)
> `impl Scheduler for SimScheduler`：`submit` 只是入队；`step` 里 `self.profiled.is_some()` 决定走 `ProfiledRuntime::step`（先 `take` 再还回，绕过借用检查）还是 `step_legacy`。

> [pegainfer-sim/src/lib.rs:643-660](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L643-L660)
> `metrics`：两条车道各自组装 `SchedulerMetrics`，但前缀缓存计数统一从同一个 `PrefixCacheTally` 报出——哪条车道收的请求，查找口径都一致（u4-l2 展开）。

回到装配点，把三个分区串起来：

> [pegainfer-sim/src/lib.rs:184-191](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L184-L191)
> `(0..partitions).map(|index| spawn_scheduler(&format!("pegainfer-sim-{index}"), SimScheduler::new(config.clone()))).collect()`：每分区一个具名线程、一份配置克隆、一个独立调度器。

#### 4.4.4 代码实践

**实践目标**：从进程外看见"每分区一线程"这个事实。

1. 操作步骤（需本地可运行环境）：启动 sim 并观察线程名：
   ```bash
   cargo run --release -p pegainfer-sim -- --port 8010 &
   PID=$(pgrep -f 'target.*pegainfer-sim' | head -1)
   ps -L -p $PID -o lwp,comm | grep sim
   ```
   （u1-l2 讲过 legacy 模式的完整启动方式；这里只需默认参数。）
2. 需要观察的现象：进程的线程列表里有一个名为 `pegainfer-sim-0` 的 OS 线程（CLI 走 `start_engine`，单分区，所以只有 `-0`）。
3. 预期结果：`ps -L` 输出至少一行 `comm` 列为 `pegainfer-sim-0`（其余是 tokio 工作线程等无名/他名线程）。改用 e2e 测试里 `start_engine_with_partitions(&config, 3)` 的方式起 3 分区，则应看到 `-0`/`-1`/`-2` 三行。**待本地验证**（本环境未运行）。
4. 若无运行条件，替代方案：阅读 [lib.rs:184-191](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L184-L191) 中 `format!("pegainfer-sim-{index}")`，回答"分区号出现在哪个字符串里、最终会被哪个工具显示"（答案：线程名；`top -H`、`ps -L`、gdb 的 `info threads`）。

#### 4.4.5 小练习与答案

**练习 1**：`SimScheduler::step` 里的 `take()`/`Some(profiled)` 模式是为什么？

答案：`ProfiledRuntime::step` 需要同时借用 `self.profiled`（可变）与 `self.queued`、`self.spec_decode`、`self.prefix_cache`（也可变）——都挂在 `self` 上，直接调用会触发借用冲突。把 `profiled` 从 `Option` 里整体 `take` 出来作为局部变量，调用完再 `Some(...)` 放回去，是 Rust 里绕开自借用冲突的常见手法（此处不追求 `&mut self` 分方法重构，因为状态就这么多，搬走反而更散）。

**练习 2**：驱动循环在空闲时为什么用 `spin_loop` 而不是 `sleep`？

答案：看 [driver.rs:94-103](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/driver.rs#L94-L103) 的注释：空闲迭代以 `spin_loop`（一条 CPU hint，放松核心的发射槽）结束就立刻再探测，忙迭代根本到不了这里——也就是说延迟路径上没有任何暂停；`sleep` 会给每一步引入唤醒延迟。sim 侧真正的"等待"发生在 `SimScheduler::step_legacy` 内部的 `park_if_waiting`（按 1ms 切片睡），那才是定时模型的归属地，循环本身永远全速。（这条约定与工作区 CLAUDE.md 的 EP free-running 纪律同源：循环永不静止等待。）

**练习 3**：如果某个分区线程 `step` 返回了 `Err`，其他分区会怎样？请求会丢吗？

答案：`drive` 会对该线程的 ledger `fail_all`——每个未结请求都拿到带真实错误的写销记账后线程退出。前端视角这个引擎下线了；兄弟分区线程仍在跑，但整机的服务连续性由前端装配层决定（u4-l4 用 `FatalScheduler` 专门测这条失败传播链，这里先记住"请求不丢错误、账户全结清"的保证来自 `fail_all`）。

## 5. 综合实践

**任务**：给 `fallback_token_id` 旋钮写一条端到端的行为测试，走通"旋钮 → 配置 → 调度器 → 输出"全链路。

**背景**：`fallback_token_id` 只在一个场合生效——请求的 prompt token 列表为空。此时 legacy 车道的输出 token 全部取该兜底值（见 [lib.rs:779-784](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L779-L784) 的 `fake_token_id`：空 prompt 直接返回 `fallback_token_id`）。

**操作步骤**：

1. 打开 [pegainfer-sim/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L802) 末尾的 `#[cfg(test)] mod tests`（注意：`collect_completion` 是这个模块的**私有**辅助函数，所以新测试必须放在同一个模块里，而不是 `tests/` 集成测试目录）。
2. 参照同模块的 [simulated_request_emits_scheduled_tokens_and_finished](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L1024-L1040)（它验证 prompt `[7, 9]` 被循环复用为输出）与 [start_engine_with_partitions_exposes_that_many_schedulers](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L1042-L1046)，在本地工作副本中加入以下测试（**示例代码**，除标注为引用的辅助函数外均为新增）：

   ```rust
   #[test]
   fn fallback_token_id_fills_empty_prompt_completion() {
       let config = SimulatedEngineConfig::new(0.0, 100.0, 0.0, 0)
           .unwrap()
           .with_fallback_token_id(7);
       let (tokens, prompt_tokens, terminal) =
           collect_completion(&config, request(vec![], 2, 0));
       assert_eq!(tokens, [7, 7]);
       assert_eq!(prompt_tokens, Some(0));
       assert!(matches!(
           terminal,
           Terminal::Finished {
               reason: FinishReason::Length,
               completion_tokens: 2,
               ..
           }
       ));
   }
   ```

3. 运行：
   ```bash
   cargo test --release -p pegainfer-sim --lib fallback_token_id -- --nocapture
   ```

**预期结果与推理**（动手前先自己推一遍）：

- `request(vec![], 2, 0)` 构造空 prompt、`max_tokens = 2` 的请求（辅助函数见 [lib.rs:820-835](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L820-L835)）；
- legacy 车道 `planned_completion`（[lib.rs:744-769](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L744-L769)）：脚本为空 → 输出 `max_tokens` 个 `fake_token_id(&[], i, 7)` → 全是 7；
- 脚本为空 → `finish_reason` 必为 `Length`（`Stop` 只属于脚本完整重放的情形）；
- 定时取 0/100/0 → 测试几乎瞬间完成，不引入真实等待；
- `collect_completion`（[lib.rs:837-865](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L837-L865)）内部 `assert_eq!(engine.schedulers.len(), 1)`——顺带复核了 `start_engine` 的单分区语义。

**需要观察的现象**：测试通过；若把 `with_fallback_token_id(7)` 换成默认（`new` 第四参给 0），断言 `[7, 7]` 应失败为 `[0, 0]`——这正是"旋钮确实流到了输出"的证据。`prompt_tokens` 断言 (`Some(0)`) 依据 `collect_completion` 对 `update.scheduled` 的记录，**待本地验证**：如失败请只保留 `tokens` 与 `terminal` 两组断言并思考 scheduled 事件对空 prompt 的语义。

**收尾提醒**：这是练习性改动，验证后请还原 `src/lib.rs`（讲义工作流不允许把测试草稿留在源码里；仓库的真实 CI 以现有测试为准）。

## 6. 本讲小结

- `SimulatedEngineConfig` 是模拟引擎全部行为的单一定义点：三个定时参数（`new` 构造期即校验：有限、非负、吞吐严格为正）加六个行为旋钮（fallback token、脚本化补全、假想投机解码、假想前缀缓存、引擎档案），`Option` 字段一律表达"该行为存在与否"，不是零值。
- `with_*` 是消费型 builder：链式、`#[must_use]`，其中只有 `with_engine_profile` 会失败（装载前全量 `validate`），因为它消费的是用户提供的文件。
- `start_engine_with_partitions(config, N)` 把配置**克隆** N 份，每份铸成一个 `SimScheduler` 并由 `spawn_scheduler` 放进一个名为 `pegainfer-sim-{i}` 的 OS 线程；`partitions` 必须与前端声明的引擎数一致，否则 serve 启动即被拒（"declared 2 engines but the launched engine exposes 1 schedulers"）。
- 装配出的 `Engine { schedulers, info, lora }` 里，`EngineInfo` 的 `kv_capacity: None` 让前端跳过 batch-fit 检查、`servable_len: None` 让 max-length 校验退回模型上下文窗口——`None` 是显式的能力声明（"入职清单"），不是遗漏。
- `Scheduler` trait 把"循环留在前端、行为留在模型线"：驱动循环负责 drain → step → metrics → commit 的固定节拍与 `fail_all` 的失败写销；模型线只需实现 `submit`/`step`/`metrics` 三个方法。
- sim 的 `step` 在两条车道间分岔的唯一依据是配置里 `profile` 是否存在——上一讲的 CLI 分岔与这里的运行时分岔是同一个开关的两端。

## 7. 下一步学习建议

下一讲（u2-l1）进入 **legacy 定时车道**的内部：`step_legacy` 如何在 admit 时一次性算好全部输出 token（`planned_completion`）、`RunningRequest.pending` 为何倒序存储、`ttft/tpot` 如何用 `next_token_at` 与 1ms 的 `WAIT_SLICE` 切片睡眠把节拍做实。本讲 4.4 已经把 `step` 的入口看清了，接下来顺着 `step_legacy` 往下读即可；建议同时对照单测 `simulated_request_emits_scheduled_tokens_and_finished`（[lib.rs:1024-1040](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L1024-L1040)）在纸上手推一遍 token 流。
