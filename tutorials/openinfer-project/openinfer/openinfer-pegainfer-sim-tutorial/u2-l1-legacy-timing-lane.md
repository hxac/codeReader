# u2-l1 legacy 定时车道：固定 TTFT/TPOT 的 step 循环

## 1. 本讲目标

学完本讲，你应该能够：

1. **推导时长模型**：解释 `ttft()` 为什么是 `base_ttft_ms + prompt_len / prefill_tokens_per_ms`，`tpot()` 为什么是一个常数，以及这两个公式分别模拟了真实引擎的哪两个阶段（prefill 与 decode）。
2. **解释 `park_if_waiting` 的切片睡眠**：说清楚为什么 `WAIT_SLICE = 1ms` 逐片睡而不是睡满整个 TTFT/TPOT——睡满会冻结准入，完全不睡会烧满一个 CPU 核。
3. **跟踪一条请求的完整事件序列**：从 `submit` 进入队列、`step_legacy` 中被 admit、TTFT 到期发出首 token、按 TPOT 节拍逐 token `push_tokens`，到最后 `Terminal::Finished` 终结。
4. 顺带理解 `RunningRequest` 的 `pending` 字段为什么「反转存储」，以及这条车道如何做到「每个 step 每个请求最多吐一个 token」——这正是与真实引擎一致的 step contract。

本讲承接 u1-l3：那里我们看过 `Scheduler` trait 只有 `submit`/`step`/`metrics` 三个方法，「循环留在前端、行为留在模型线」。本讲就钻进 `SimScheduler` 在**没有配置 `--profile` 档案**时的那条实现路径——legacy 定时车道。

## 2. 前置知识

### 2.1 TTFT 与 TPOT（复习并公式化）

u1-l1 引入过这两个指标，这里给出 sim 使用的精确形式：

- **TTFT**（Time To First Token，首 token 延迟）：从请求被引擎接受到第一个输出 token 产生的时间。在真实引擎里它由 **prefill 阶段**（一次性读完整个 prompt、填充 KV cache）决定。
- **TPOT**（Time Per Output Token，逐 token 耗时）：之后每多生成一个 token 的间隔。在真实引擎里它由 **decode 步长**（每步为一个序列产出一个 token）决定。

sim 的 legacy 车道把这两件事抽象成两个可配置的时长：

\[ \mathrm{TTFT} = \mathrm{base\_ttft\_ms} + \frac{\mathrm{prompt\_len}}{\mathrm{prefill\_tokens\_per\_ms}} \]

\[ \mathrm{TPOT} = \mathrm{tpot\_ms} \]

直觉：`base_ttft_ms` 是「无论 prompt 多长都要付的固定启动开销」（kernel 启动、调度等），第二项把 prompt 想象成以 `prefill_tokens_per_ms` 的恒定吞吐被消费——prompt 越长首 token 越晚。TPOT 则完全不依赖序列内容，是一个常数节拍。

### 2.2 Rust 的 `Instant` 与 `Duration`

- `Instant::now()` 返回单调时钟上的一个时刻（不受系统改时间影响），`Instant + Duration` 得到未来一个时刻，两个 `Instant` 相减得 `Duration`。
- `Duration` 内部是「秒 + 纳秒」，构造自 f64 毫秒时要用 `Duration::try_from_secs_f64(ms / 1000.0)`；负数或超出表示范围的值会转换失败。

### 2.3 f64 的两个陷阱：无穷与 NaN

`base_ttft_ms` 等参数是 `f64`。除以 0 会得到 `inf`，`0/0` 会得到 `NaN`，而 `Duration` 无法表示它们。所以你在源码里会看到**两层防线**：`SimulatedEngineConfig::new` 在构造期校验参数，`duration_from_ms` 在换算期再校验一次（见 4.1）。

### 2.4 驱动线程模型（谁在调用 step）

u1-l3 说过「循环留在前端」。具体地，`spawn_scheduler`（pegainfer-sim 侧调用点在 u1-l3 讲过的 `start_engine_with_partitions` 里）会为每个调度器启动一个 OS 线程，运行前端的 `drive` 循环：**排空提交通道 → 调用一次 `scheduler.step(&mut ledger)` → 发布 metrics → 提交本步账本**，如此往复。`step` 是同步阻塞调用，所以 sim 在 `step` 内部「睡觉」来模拟 GPU 耗时——这是允许的，因为每个分区只有这一个驱动线程，睡觉恰恰就是在「占用虚拟的 GPU」。

`RequestLedger`（账本）是前端借给调度器的写入接口：`admit`、`push_tokens`、`finish`、`retire` 等方法只是记账，真正把这一步的 token/终结事件打包发给协议栈的，是驱动循环在 step 结束后的 `commit_step`。

## 3. 本讲源码地图

本讲的核心源码只有一个文件，但会在两处「越界」看一眼前端：

| 文件 | 作用 | 本讲关注的区域 |
|------|------|----------------|
| `pegainfer-sim/src/lib.rs` | sim 引擎全部核心逻辑 | `WAIT_SLICE` 常量、`ttft`/`tpot`/`duration_from_ms`、`RunningRequest`、`step_legacy`、`park_if_waiting`、`collect_completion` 测试辅助 |
| `pegainfer-frontend/src/engine/driver.rs`（只读参考） | 前端驱动循环与 `Scheduler` trait 定义 | `drive` 主循环——确认「谁在调 step、新请求何时被 drain」 |

`src/lib.rs` 内部区域速查（行号基于当前 HEAD）：

| 行号 | 内容 |
|------|------|
| L40–L43 | `WAIT_SLICE` 常量及其设计理由注释 |
| L148–L154 | `ttft()` / `tpot()` |
| L261–L277 | `SimScheduler` 与 `RunningRequest` 结构体 |
| L607–L616 | `park_if_waiting` |
| L619–L641 | `Scheduler` trait 实现（车道分岔点） |
| L672–L740 | `step_legacy`（本讲主角） |
| L744–L784 | `planned_completion` / `fake_token_id`（u2-l2 深入，本讲只看交界处） |
| L786–L793 | `duration_from_ms` |
| L837–L865 | `collect_completion` 测试辅助 |

## 4. 核心概念与源码讲解

### 4.1 时长模型：`ttft` / `tpot` / `duration_from_ms`

#### 4.1.1 概念说明

legacy 车道的一切行为都由三个数字驱动：`base_ttft_ms`（固定启动开销）、`prefill_tokens_per_ms`（prefill 吞吐）、`tpot_ms`（decode 节拍）。它们在 u1-l3 讲过的 `SimulatedEngineConfig::new` 里构造期校验（非负、有限、吞吐严格为正），本讲关心的是它们如何被**换算成 `Duration`** 供时间线使用。

`duration_from_ms` 是唯一的换算通道，它同时承担防御职责：f64 世界里 `inf`/`NaN`/负数都可能混进来，而 `Duration` 不接受它们——与其在运行期 panic，不如返回 `Err` 让调度器的 `step` 把错误上抛（前端会把它当致命错误处理，见 u4-l4）。

#### 4.1.2 核心流程

```
ttft(prompt_len):
    毫秒值 = base_ttft_ms + prompt_len / prefill_tokens_per_ms
    return duration_from_ms(毫秒值)          # 校验 + 换算

tpot():
    return duration_from_ms(tpot_ms)          # 同一通道

duration_from_ms(ms):
    1. ensure ms 有限且 >= 0        # 拦 inf / NaN / 负数
    2. Duration::try_from_secs_f64(ms / 1000.0)  # 拦超出 Duration 表示范围的值
    3. 失败时附加上下文返回 Err
```

速算表（默认配置 `base=5.0`、`100.0 tok/ms`，见 `Default` 实现）：

| prompt 长度 | TTFT |
|------------|------|
| 0 token | 5 ms |
| 500 token | 5 + 5 = 10 ms |
| 2000 token | 5 + 20 = 25 ms |

#### 4.1.3 源码精读

[pegainfer-sim/src/lib.rs:L148-L154](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L148-L154)——两个时长查询方法，都是私有方法，只被 legacy 车道使用：

```rust
fn ttft(&self, prompt_tokens: usize) -> Result<Duration> {
    duration_from_ms(self.base_ttft_ms + prompt_tokens as f64 / self.prefill_tokens_per_ms)
}

fn tpot(&self) -> Result<Duration> {
    duration_from_ms(self.tpot_ms)
}
```

`ttft` 是公式的直接翻译：固定项 + prompt 长度除以吞吐。注意它返回 `Result`——派生值（和、商）可能失去原始参数的良构性，所以换算函数有权拒绝。

[pegainfer-sim/src/lib.rs:L786-L793](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L786-L793)——唯一的 f64→Duration 通道：

```rust
fn duration_from_ms(ms: f64) -> Result<Duration> {
    ensure!(
        ms.is_finite() && ms >= 0.0,
        "timing value must be finite and non-negative"
    );
    Duration::try_from_secs_f64(ms / 1000.0)
        .context("timing value is not representable as a Duration")
}
```

两道闸门：`ensure!` 拦非有限值和负数；`try_from_secs_f64` 拦「有限但大到 `Duration` 装不下」的值。两者都变成带上下文的 `anyhow` 错误。

[pegainfer-sim/src/lib.rs:L157-L170](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L157-L170)——`Default` 给出的缺省定时：`base_ttft_ms: 5.0`、`prefill_tokens_per_ms: 100.0`、`tpot_ms: 12.0`。CLI 不传定时参数时（u1-l2 讲过的 `DEFAULT_*` 常量）最终落点与这组值对应。

[pegainfer-sim/src/lib.rs:L1015-L1022](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L1015-L1022)——构造期防线的测试：`config_rejects_invalid_timing_values` 用六个断言逐一覆盖负 base、零吞吐、负 TPOT、NaN、正无穷 base、正无穷 TPOT。**注意零 TPOT 不在其中**——`tpot_ms = 0.0` 是合法配置，它把 decode 节拍归零，是「零成本」测试（如 L1025 的测试）能瞬间跑完的前提。

#### 4.1.4 代码实践

1. **实践目标**：把 4.1.3 的两道防线和测试一一对应起来。
2. **操作步骤**：在工作区根目录运行

   ```bash
   cargo test --release -p pegainfer-sim --lib config_rejects_invalid_timing_values -- --nocapture
   ```

   （回忆 u1-l1：工作区 default-members 不含 sim，必须带 `-p pegainfer-sim`。）
3. **需要观察的现象**：测试通过；`--nocapture` 下无额外输出（该测试只有断言）。
4. **预期结果**：通过后，逐行核对六个 `assert!(...is_err())` 分别对应 `SimulatedEngineConfig::new` 里哪个 `ensure!`（负/非有限 base、非正吞吐、负/非有限 TPOT——L75–L86 区间的三段校验各出现两次）。
5. 本实践为已存在测试的运行与阅读，可直接复现。

#### 4.1.5 小练习与答案

**练习 1**：默认配置下，2000 token 的 prompt 首 token 多久之后出现？
**答案**：\( 5 + 2000/100 = 25 \) ms。

**练习 2**：为什么 `prefill_tokens_per_ms` 要求**严格为正**，而 `tpot_ms` 只要求非负？
**答案**：吞吐出现在**分母**上，为 0 时 `prompt_len / 0` 会产生 `inf`（或空 prompt 时 `0/0 = NaN`），虽然 `duration_from_ms` 也会在换算期拦住它，但构造期就拒绝能把错误提前到配置面（fail-fast，与 u1-l2 讲过的互斥校验同一哲学）；TPOT 为 0 则是良构的「零成本 decode」，测试大量依赖它。

**练习 3**：`new` 已经校验过参数，`duration_from_ms` 为什么还要再校验一遍？
**答案**：防御纵深。`new` 校验的是**原始参数**；`ttft()` 换算的是**派生值**（和、商），且 `Duration::try_from_secs_f64` 对「有限但超大」的值同样会失败——第二道闸门覆盖的是第一道管不到的输入。

### 4.2 `step_legacy` 全景：admit 循环与 decode 循环

#### 4.2.1 概念说明

`step_legacy` 是 `Scheduler::step` 在未配置档案时的实现，是 legacy 车道的全部行为。它每次被前端驱动线程调用时做三件事：

1. **准入（admit）**：把 `submit` 攒下的队列一次排空——决定这个请求输出什么（`planned_completion` 的交界处，u2-l2 详讲）、记账 `admit`、按 TTFT 预约首 token 时刻；
2. **推进（decode）**：遍历所有在跑请求，把「到期了」的各吐一个 token，没到期的保留，吐完的终结；
3. **停靠（park）**：如果所有请求的下一个 token 都还没到期，小睡一会再返回（4.4 详讲）。

先看分岔点。[pegainfer-sim/src/lib.rs:L619-L641](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L619-L641)——trait 实现：`submit` 只是入队（真正的请求来自前端驱动线程排空提交通道后转交过来）；`step` 检查 `self.profiled.is_some()` 决定走哪条车道：

```rust
fn submit(&mut self, request: QueuedRequest) {
    self.queued.push(request);
}

fn step(&mut self, ledger: &mut RequestLedger) -> Result<()> {
    if self.profiled.is_some() {
        let mut profiled = self.profiled.take().expect(/* ... */);
        let result = profiled.step(&self.config, &mut self.queued, ledger,
                                    &mut self.spec_decode, &mut self.prefix_cache);
        self.profiled = Some(profiled);
        return result;
    }
    self.step_legacy(ledger)
}
```

（`take()` 再放回去的绕行是为了把 `ProfiledRuntime` 整个借出来、同时还能传 `&self.config` 等字段，避免 `&mut self` 的双重借用。）

而调用方循环在 [pegainfer-frontend/src/engine/driver.rs:L67-L84](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/driver.rs#L67-L84)：

```rust
loop {
    loop {  // 排空提交通道：register 进账本，转手给 scheduler.submit
        match submissions.try_recv() { /* ... */ }
    }
    let step = scheduler.step(&mut ledger);        // ← step_legacy 在这里被调
    let snapshot = scheduler.metrics();
    let metrics_changed = metrics.publish(&snapshot);
    ledger.commit_step(metrics_changed);           // ← 本步事件此刻才发给协议栈
    // step 出错 → fail_all 并退出驱动线程（u4-l4 详讲）
}
```

**关键结论**：新提交的请求只在**两次 step 调用之间**被 drain 进 `self.queued`。这正是 `WAIT_SLICE` 存在的根源（4.4）。

#### 4.2.2 核心流程

`step_legacy` 的两个循环 + 一次停靠（对应 [L672-L740](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L672-L740)）：

```
step_legacy(ledger):
  # ── 循环 A：准入 ──
  for (id, request) in queued.drain(..):
      if ledger.is_aborted(id): ledger.retire(id); 跳过     # 客户端已放弃
      prefix_cache.note(...)                                 # 假想前缀查找（u4-l2）
      (pending, finish_reason) = planned_completion(...)     # 决定输出内容（u2-l2）
      ledger.admit(id)
      if 请求要 prompt_logprobs: ledger.echo_prompt(...)
      if pending 为空: ledger.finish(id, finish_reason); 跳过  # 如 max_tokens=0
      running.push(RunningRequest {
          next_token_at = Instant::now() + ttft(prompt_len), # ← TTFT 在准入时刻被「烤」进时间线
          ...
      })

  # ── 循环 B：推进 ──
  now = Instant::now()
  for running in running.drain(..):          # 本步刚准入的也在内！
      if ledger.is_aborted: ledger.retire(id); 跳过
      if now < next_token_at: 保留; 跳过      # 未到期
      token = pending.pop()
      if 取不到: ledger.finish(...); 跳过      # 防御分支（见练习 3）
      （可选 logprobs 夹具 / 投机解码计数）
      ledger.push_tokens(id, [token], ...)
      if pending 空了: ledger.finish(id, finish_reason)      # 终结，不再保留
      else: next_token_at = Instant::now() + tpot(); 保留    # ← 以「实际吐出时刻」重排节拍

  # ── 停靠 ──
  park_if_waiting()
```

一条请求（prompt = `[7, 9, 11]`，`max_tokens = 3`，`base_ttft = 10ms`，`tpot = 50ms`，无脚本）的生命周期时间线（以准入时刻为 t=0）：

| 时刻 | 发生的事 | 账本写入 |
|------|---------|---------|
| t=0（某次 step 的循环 A） | `planned_completion` 生成 3 个待发 token；`next_token_at = t + (10 + 3/100)ms ≈ t+10ms` | `admit` |
| t≈10ms（某次 step 的循环 B） | 首 token 到期，弹出并发出；`next_token_at = t'+50ms`（t' 为实际发出时刻） | `push_tokens([token1])` |
| t≈60ms | 第二个 token | `push_tokens([token2])` |
| t≈110ms | 第三个 token；`pending` 空，同一步内终结 | `push_tokens([token3])` + `finish(Length)` |
| 该次 step 返回后 | 前端 `commit_step` 把上面事件打包发给协议栈 | —— |

三个值得刻进脑子的结构性质：

1. **每个 step 每个请求最多吐一个 token**——循环 B 对每个请求最多 `pop` 一次。这与真实引擎「一个 decode 步为批内每个序列产出一个 token」的 step contract 一致；
2. **同一 step 内可完成「准入即吐首 token」**——循环 A 先运行，新请求已进 `self.running`，循环 B 用**新鲜的** `Instant::now()`（L701）比较，TTFT≈0 时立即到期。零成本测试正是靠这一点几步跑完；
3. **TPOT 从实际吐出时刻重新起算**（L733 用 `Instant::now()` 而不是循环开头的 `now`）——节拍是「发出到发出」的间隔，不受同批其他请求的数量影响。

#### 4.2.3 源码精读

[pegainfer-sim/src/lib.rs:L672-L699](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L672-L699)——准入循环。`drain(..)` 一次性拿走整个队列（`std::mem::take` 语义），逐个做中止检查、假想前缀查找、输出规划、记账：

```rust
for QueuedRequest { id, request } in self.queued.drain(..) {
    if ledger.is_aborted(id) {
        ledger.retire(id);
        continue;
    }
    // The pretend lookup happens exactly where a real engine would do
    // it: once, on admission.
    self.prefix_cache.note(self.config.prefix_cache, &request);
    let prompt_len = request.prompt_tokens.len();
    let (pending, finish_reason) =
        planned_completion(&self.config, &request.prompt_tokens, request.max_tokens);
    ledger.admit(id);
    // ... prompt_logprobs 回显（略） ...
    if pending.is_empty() {
        ledger.finish(id, finish_reason);
        continue;
    }
    self.running.push(RunningRequest {
        id,
        pending,
        next_token_at: Instant::now() + self.config.ttft(prompt_len)?,
        // ...
    });
}
```

两个细节：`pending` 为空（例如 `max_tokens = 0`）的请求**准入即终结**，从不进入 `running`；`next_token_at` 在准入时刻就算好——TTFT 是「预约制」，之后每步只是对表。

[pegainfer-sim/src/lib.rs:L701-L737](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L701-L737)——推进循环。`drain` + 重建 `still_running` 的写法让「移出」天然发生：终结/中止的请求不 push 回去，其余保留：

```rust
let now = Instant::now();
let mut still_running = Vec::new();
for mut running in self.running.drain(..) {
    if ledger.is_aborted(running.id) {
        ledger.retire(running.id);
        continue;
    }
    if now < running.next_token_at {
        still_running.push(running);
        continue;
    }
    let Some(token) = running.pending.pop() else {
        ledger.finish(running.id, running.finish_reason);
        continue;
    };
    // ... logprobs 夹具与投机解码计数（略） ...
    ledger.push_tokens(running.id, &[token], &logprobs);
    if running.pending.is_empty() {
        ledger.finish(running.id, running.finish_reason);
    } else {
        running.next_token_at = Instant::now() + self.config.tpot()?;
        still_running.push(running);
    }
}
self.running = still_running;
self.park_if_waiting();
```

注意 `let Some(token) = ... else { finish }` 这个分支实际**不可达**（练习 3 会问为什么），它是对「空 pending 的请求不该留在 running」这一不变量的防御性守卫。中止检查在两个循环里各出现一次——legacy 车道的取消语义就这两行 `retire`，深度对比留给 u2-l5。

[pegainfer-sim/src/lib.rs:L643-L659](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L643-L659)——`metrics` 的 legacy 分支：`num_running_reqs = self.running.len()`、`num_waiting_reqs = self.queued.len()`。前端驱动循环靠这两个数判断调度器是否空闲（L94 起的 idle 分支），所以「已准入但还没吐首 token」的请求在指标上已经是 running——与 vLLM 的语义一致。

#### 4.2.4 代码实践

1. **实践目标**：亲手跟踪一条请求的事件序列，验证「准入 → 到期吐 token → 节拍 → 终结」。
2. **操作步骤**：阅读 [pegainfer-sim/src/lib.rs:L837-L865](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L837-L865) 的 `collect_completion` 辅助函数，它就是「前端驱动循环」的测试替身：`start_engine` 起一个分区、`take_steps` 拿到 step 流、`handle.submit(req)` 提交请求（注意这是前端 `SchedulerHandle::submit`，不是 `Scheduler::submit`，中间隔着驱动线程的通道）、循环收 step 直到出现 terminal。然后运行现成的行为锚定测试：

   ```bash
   cargo test --release -p pegainfer-sim --lib simulated_request_emits -- --nocapture
   ```

3. **需要观察的现象**：测试通过；它断言 prompt `[7,9]`、`max_tokens=3` 时输出恰为 `[7, 9, 7]`、`prompt_tokens == Some(2)`（`admit` 对应的 `scheduled` 事件）、终结为 `Terminal::Finished { reason: Length, completion_tokens: 3 }`。
4. **预期结果**：把断言逐条对到 4.2.2 时间线表的账本写入上——`admit` 对应 `scheduled`，每个 `push_tokens` 对应流里的 tokens 增量，`finish` 对应 terminal。
5. 本实践为已存在测试的运行与阅读，可直接复现。

#### 4.2.5 小练习与答案

**练习 1**：`max_tokens = 0` 的请求在 legacy 车道会发生什么？
**答案**：`planned_completion` 的 `emit_count` 为 0，返回空 `pending`；准入循环里命中 `if pending.is_empty()` 分支（L688–L691），**同一步内** `admit` 后立即 `finish`，从不进入 `running`，TTFT/TPOT 与它无关。

**练习 2**：同一个 `step` 调用里，刚 admit 的请求可能立刻发出首 token 吗？
**答案**：可能。循环 A 先于循环 B 执行，新请求已 push 进 `self.running`；循环 B 开头重新取 `Instant::now()`（L701），只要 TTFT 换算出的时长小于循环 A 到 B 之间流逝的时间（比如 TTFT=0.03ms），首 token 就在**准入的同一个 step**里发出。零成本测试依赖这一点快速收敛。

**练习 3**：循环 B 里 `let Some(token) = running.pending.pop() else { finish }` 什么时候触发？
**答案**：实际不可达，是防御性守卫。不变量是「pending 为空的请求不留在 running」：准入时空 pending 会立即 finish（L688）；发出最后一个 token 的同一步也会 finish（L730–L731）。如果未来改动破坏了这个不变量，这个分支会把请求体面终结而不是 panic 或静默卡死。

### 4.3 `RunningRequest` 与 `pending` 反转存储

#### 4.3.1 概念说明

`RunningRequest` 是「一条已被引擎接受、正在按节拍吐 token 的请求」在调度器里的全部状态。它不保存原始 `Request`（那太大），只保存推进所必需的四样东西：身份、待发 token、下一个到期时刻、终结原因（外加可选的 logprobs 档位）。

最有趣的设计是 `pending` 字段：**待发 token 按发出顺序的反方向存放**。`planned_completion` 在生成时是正序的 `Vec<u32>`，返回前做一次 `reverse()`；此后每次发 token 只需 `pop()`——从 Vec 尾部取元素是 O(1)。如果正序存放，就得 `remove(0)`（O(n) 整体搬移）或改用 `VecDeque`。一次 `reverse` 换来整条生命周期的常数弹出，是典型的「构造期付一次成本」。

#### 4.3.2 核心流程

```
planned_completion:                       # u2-l2 详讲，这里只看交界
    emit_count = max_tokens（无脚本时）
    pending = [第0个, 第1个, ..., 第emit_count-1个]   # 正序
    pending.reverse()                     # ← 反转：栈顶（Vec 尾）= 下一个该发的
    return (pending, finish_reason)

step_legacy 循环 B:
    token = running.pending.pop()         # O(1)，取到的正是「下一个」token
```

无脚本时第 i 个 token 由 `fake_token_id(prompt, i, fallback)` 决定：prompt 非空时循环取 `prompt[i % len]`，空 prompt 用 fallback（[L779-L784](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L779-L784)）。所以无脚本模式下「引擎的输出」就是把 prompt 自己循环复读——这足以驱动前端全链路，又完全可预测。

[pegainfer-sim/src/lib.rs:L744-L769](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L744-L769) 的函数把「发什么」与「何时发」解耦：`planned_completion` 只管内容与 `FinishReason`（脚本重放完 → `Stop`；被 `max_tokens` 截断或无脚本 → `Length`），`step_legacy` 只管时间线。这个解耦让 u2-l2 可以单独深讲内容、本讲单独深讲时间。

#### 4.3.3 源码精读

[pegainfer-sim/src/lib.rs:L261-L277](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L261-L277)——两个结构体。`SimScheduler` 持有配置、两个队列（`queued` 等待准入、`running` 已在跑）与三件可选设施；`RunningRequest` 的 `pending` 注释一语道破存储方向：

```rust
struct RunningRequest {
    id: RequestId,
    /// Tokens not yet emitted, stored reversed for cheap pops.
    pending: Vec<u32>,
    next_token_at: Instant,
    finish_reason: FinishReason,
    logprobs: Option<usize>,
}
```

`finish_reason` 在**准入时刻**就定好了：legacy 车道没有真采样，输出序列从一开始就完全已知，终结原因只是这条已知序列的属性（脚本是否放完），不是运行中决定的。

[pegainfer-sim/src/lib.rs:L760-L768](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L760-L768)——反转就发生在这里：

```rust
    let mut pending: Vec<u32> = if script.is_empty() {
        (0..emit_count)
            .map(|index| fake_token_id(prompt_tokens, index, config.fallback_token_id))
            .collect()
    } else {
        script[..emit_count].to_vec()
    };
    pending.reverse();
    (pending, finish_reason)
```

[pegainfer-sim/src/lib.rs:L912-L918](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L912-L918)——`fake_token_id_cycles_prompt_tokens` 直接锚定取值规则（含空 prompt 走 fallback 的分支）。

#### 4.3.4 代码实践

1. **实践目标**：用一个**非回文**输出序列证明「发出顺序 = 脚本/复读的正序」，即反转存储没有被弄反。
2. **操作步骤**：在 `src/lib.rs` 的 `#[cfg(test)] mod tests` 里（`collect_completion` 是模块私有辅助，测试必须放这里）**临时添加**一个测试后运行（这是阅读用源码仓库的本地实验，验证后还原即可）：

   ```rust
   #[test]
   fn legacy_emission_order_is_prompt_cycle_order() {
       let config = SimulatedEngineConfig::new(0.0, 100.0, 0.0, 42).unwrap();
       let (tokens, _, terminal) = collect_completion(&config, request(vec![7, 9, 11], 5, 0));
       assert_eq!(tokens, vec![7, 9, 11, 7, 9]);   // index%3: 0,1,2,0,1
       assert!(matches!(
           terminal,
           Terminal::Finished { reason: FinishReason::Length, completion_tokens: 5, .. }
       ));
   }
   ```

   ```bash
   cargo test --release -p pegainfer-sim --lib legacy_emission_order -- --nocapture
   ```

3. **需要观察的现象**：测试通过；输出 `[7, 9, 11, 7, 9]` 而不是它的反转 `[9, 7, 11, 9, 7]`。
4. **预期结果**：通过即证明 `reverse()` + `pop()` 的组合保持了正序发出。若有人把 `pending.reverse()` 删掉，此测试会以 `[9, 7, 11, 9, 7]` 失败——而现有测试 `simulated_request_emits_scheduled_tokens_and_finished` 用的 `[7,9,7]` 恰是回文，删掉 reverse 它照样通过（练习 3 会让你解释这个盲区）。
5. 本实践为读者自写测试，行为推导自 `fake_token_id` 的定义（`prompt[index % len]`），结论确定性高；若在本地得到不同输出，优先检查是否抄错了 prompt 或 max_tokens。

#### 4.3.5 小练习与答案

**练习 1**：为什么用「反转的 Vec + pop」而不是正序 Vec 的 `remove(0)` 或 `VecDeque`？
**答案**：`remove(0)` 每次要整体搬移后面所有元素，O(n)；`VecDeque` 能做到 O(1) 但引入双端队列的额外复杂度。输出序列在准入时就完全已知，「一次 reverse + 尾部 pop」用最简单的容器拿到 O(1) 弹出。

**练习 2**：prompt = `[5]`、`max_tokens = 4`、无脚本时输出什么？空 prompt、`fallback = 42` 时呢？
**答案**：`[5, 5, 5, 5]`（`index % 1` 恒为 0，复读唯一元素）；空 prompt 时 `fake_token_id` 走 fallback 分支，输出 `[42, 42, 42, 42]`——这也是 u1-l3 提到 `with_fallback_token_id` 存在的原因。

**练习 3**：现有测试为什么测不出「误删 `pending.reverse()`」这种 bug？
**答案**：它用 prompt `[7, 9]`、`max_tokens=3`，输出 `[7, 9, 7]` 是**回文**，反转后不变。要锚定顺序就必须用非回文序列（如 `[7, 9, 11]` 配 `max_tokens=5`，得 `[7, 9, 11, 7, 9]`）。这是一个真实的测试盲区案例：断言值选得「太对称」会掩盖方向性错误。

### 4.4 `park_if_waiting` 与 `WAIT_SLICE` 切片睡眠

#### 4.4.1 概念说明

循环 B 结束后，若还有请求在跑但下一个 token 都没到期，`step_legacy` 就要回答一个问题：**现在返回吗？**

两个极端都不可取：

- **立刻返回**：前端驱动循环会马上再调一次 `step`。注意它的空转缓冲有前提——[driver.rs:L94-L103](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/driver.rs#L94-L103) 只有在 `num_running == 0 && num_waiting == 0` 时才 `spin_loop` 让出发射槽。只要有一条请求在等它的 50ms TPOT，metrics 就报告 running ≥ 1，驱动线程会在两个到期时刻之间**全速空转烧满一个核**。
- **睡满整段 TPOT/TTFT**：新请求只在两次 step 之间被 drain（4.2.1 的驱动循环结构）。睡 50ms 甚 至几百 ms 意味着这期间到达的请求全部堵在通道里——它们的实际 TTFT 被人为抬高，客户端 abort 的检测也被拖延。对一个用来「验证前端行为」的 sim，这会系统性扭曲它要测的东西。

`WAIT_SLICE = 1ms` 是折中：每步最多睡 1ms，CPU 不空转，同时新请求最多等 1ms 就能被下一次循环准入。源码注释把这两点写得很明白（L40–L43）。

#### 4.4.2 核心流程

```
park_if_waiting():
    next = min(所有 running 请求的 next_token_at)   # 没有在跑请求 → None
    if next 不存在: return                          # 无事可等，立即返回
    now = Instant::now()
    if next <= now: return                          # 已有 token 到期，别睡
    sleep( min(next - now, WAIT_SLICE) )            # 至多 1ms
```

配合循环 B 的到期检查，正确的「等到了再发」语义由两者合成保证：park 可能提前 1ms 醒来，但醒后 `step` 被再次调用，循环 B 的 `now < next_token_at` 检查会把未到期请求继续保留——**睡眠只是节能，到期判定只认 `Instant` 比较**。这也意味着 token 间隔的下界是精确的（到期才发），上界受切片粒度影响（最多晚约 1ms + 调度噪声）。

顺带一提：档案车道复用同一常量——`ProfiledRuntime::step` 里对 `in_flight.ready_at` 未到期的处理同样是 `sleep(min(剩余, WAIT_SLICE))`（[L323-L333](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L323-L333)），u3-l4 会再遇到它。

#### 4.4.3 源码精读

[pegainfer-sim/src/lib.rs:L40-L43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L40-L43)——常量与它的完整设计理由（注释原文即 4.4.1 的两个极端）：

```rust
/// Cap on how long `step` parks while waiting for the next due token. New
/// submissions only drain between steps, so a full TTFT/TPOT sleep would
/// stall admission; 1ms keeps the CPU-only sim from spinning a core.
const WAIT_SLICE: Duration = Duration::from_millis(1);
```

[pegainfer-sim/src/lib.rs:L607-L616](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L607-L616)——实现只有 8 行，注意它是 `&self`（纯读取，不修改状态），且取的是**所有在跑请求的最早到期时刻**（批量视角：一批请求里最近的那个 deadline 决定还要等多久）：

```rust
fn park_if_waiting(&self) {
    let Some(next) = self.running.iter().map(|r| r.next_token_at).min() else {
        return;
    };
    let now = Instant::now();
    if next <= now {
        return;
    }
    std::thread::sleep((next - now).min(WAIT_SLICE));
}
```

`running` 为空时直接返回——此时驱动循环自己会走到 idle 分支的 `spin_loop` 提示，停靠的责任交还给前端。`std::thread::sleep` 保证不早醒（最多晚醒），这保证了 4.2.2 里「token 间隔 ≥ TPOT」的下界论证成立。

#### 4.4.4 代码实践

1. **实践目标**：体会 `WAIT_SLICE` 对「迟到请求的 TTFT」的影响。
2. **操作步骤**（源码阅读 + 推演型实践）：
   - 先做思想实验：假设把 `WAIT_SLICE` 改成 `Duration::from_millis(100)`。请求 A（`tpot = 50ms`，`max_tokens = 5`）正在跑，请求 B 在 A 的第 2、3 个 token 之间到达。写出 B 的实际首 token 时刻相对其 TTFT 公式值的偏差区间。
   - 若本地可改代码验证：临时改常量、用一个「先提交 A、睡眠 60ms 后提交 B（`base_ttft = 0`、prompt 很短）」的测试观察 B 的 wall-clock 首 token 延迟，**验证后还原**。
3. **需要观察的现象**（思想实验答案自查）：B 至少要等到当前这一次 `step` 返回（最坏还剩约 100ms 的睡眠），再经过 drain → 准入 → 下一次循环 B 才能吐首 token；即偏差上界约为一个 WAIT_SLICE（100ms）量级，而默认 1ms 时同样场景偏差 ≤ 约 1–2ms。
4. **预期结果**：结论是「WAIT_SLICE 是 sim 自身给准入延迟引入的人为上界」——这就是注释里 "stall admission" 的具体含义。思想实验部分无需运行即可完成；改代码验证部分**待本地验证**（涉及线程时序，观察值有抖动，看量级而非精确值）。
5. 若选择不改代码，本实践以推演 + 对照 L40–L43 注释完成即为达标。

#### 4.4.5 小练习与答案

**练习 1**：把 park 的睡眠从「切片」改成「睡满 `next - now`」，具体破坏什么？
**答案**：新请求只在两次 step 之间被驱动循环 drain（driver.rs 内层 `try_recv` 循环）。睡满意味着睡眠期间到达的请求全部滞留在通道里，实际 TTFT 被抬高至多整整一段 TTFT/TPOT；同时 `is_aborted` 检查也随 step 停摆，abort 响应变慢。对以「验证前端行为」为目的的 sim，这是自我扭曲。

**练习 2**：那干脆不睡、直接返回行不行？
**答案**：不行。metrics 报告 `num_running_reqs ≥ 1` 时，驱动循环的 idle 缓冲（`spin_loop`）不触发，驱动线程会在 token 期限之间全速循环调用 `step`，烧满一个核——注释里 "keeps the CPU-only sim from spinning a core" 说的就是这半边。

**练习 3**：park 提前 1ms 醒来时，token 会不会被提前发出？
**答案**：不会。park 只是睡眠，不做任何发 token 的决定；醒来后 `step` 被再次调用，循环 B 用 `now < running.next_token_at` 严格把关，未到期就继续保留。到期判定唯一的依据是 `Instant` 比较，所以 token 间隔的下界精确等于 TPOT，上界多出约一个切片粒度的延迟。

## 5. 综合实践

把本讲四个模块串成一个测试：**同一份输出序列，验证「零定时下完整输出 + Length 终结」与「TPOT 节拍下的墙钟时间」两个面**。

任务：在 `src/lib.rs` 的 `#[cfg(test)] mod tests` 中添加如下测试（`Duration` 与 `Instant` 已在 lib.rs 顶部具名引入（L2–L3），测试模块的 `use super::*` 使它们直接可用）：

```rust
#[test]
fn legacy_lane_paces_tokens_with_tpot() {
    // 第一段：base_ttft=0、tpot=0 —— 所有 token 依次就绪，输出完整且以 Length 终结
    let zero = SimulatedEngineConfig::new(0.0, 100.0, 0.0, 42).unwrap();
    let (tokens, _, terminal) = collect_completion(&zero, request(vec![7, 9, 11], 5, 0));
    assert_eq!(tokens, vec![7, 9, 11, 7, 9]);        // fake_token_id 的复读规则
    assert!(matches!(
        terminal,
        Terminal::Finished { reason: FinishReason::Length, completion_tokens: 5, .. }
    ));

    // 第二段：tpot=50ms —— 首 token 立即就绪，其后每个间隔 ≥ 50ms
    let paced = SimulatedEngineConfig::new(0.0, 100.0, 50.0, 42).unwrap();
    let started = Instant::now();
    let (tokens, _, terminal) = collect_completion(&paced, request(vec![7, 9, 11], 3, 0));
    let elapsed = started.elapsed();
    assert_eq!(tokens, vec![7, 9, 11]);
    assert!(matches!(
        terminal,
        Terminal::Finished { reason: FinishReason::Length, .. }
    ));
    // 3 个 token = 2 个间隔：总时长 ≥ 2 × 50ms（睡眠不早醒 + 到期才发的双保险）
    assert!(elapsed >= Duration::from_millis(100));
    // 上界给 CI 抖动与睡眠精度留余量（每段 50ms 最多被 park 切片与调度噪声拉长）
    assert!(elapsed < Duration::from_millis(250));
}
```

运行：

```bash
cargo test --release -p pegainfer-sim --lib legacy_lane_paces_tokens_with_tpot -- --nocapture
```

要点与检查清单：

1. **下界 100ms 的论证**（把 4.2 与 4.4 串起来）：首 token 因 TTFT = 0 + 3/100 ms ≈ 0 而立即发出；之后每次 `next_token_at = 发出时刻 + 50ms`，而循环 B 只在 `now >= next_token_at` 时才发（L708），`thread::sleep` 又保证 park 不早醒——所以每个间隔严格 ≥ 50ms，3 个 token 总时长 ≥ 100ms。墙钟数 ≈ (max_tokens − 1) × tpot，而不是 max_tokens × tpot，因为首 token 不经过任何 TPOT 等待。
2. **上界是工程判断**：默认 `WAIT_SLICE = 1ms` 下每个 50ms 间隔被 ~50 次切片睡眠覆盖，Linux 上睡眠通常只晚醒不早醒；250ms 的上限给重载 CI 留了约一倍余量。若在很慢的机器上偶尔越界，放宽上界前先打印 `elapsed` 看量级。
3. 想看得更直白，可在测试里加一行 `println!("elapsed: {elapsed:?}");`（配合 `--nocapture`），观察它落在 100ms 出头。
4. 本实践为读者自写测试；断言值由本讲源码推导（`fake_token_id` 规则、L708 到期检查、L733 节拍重排），推导链已在第 1 点给出。若本地行为与推导不符，按 4.2.2 的时间线表逐步定位是哪一环的预期错了。

## 6. 本讲小结

- **时长模型**：legacy 车道用 \( \mathrm{TTFT} = \mathrm{base\_ttft\_ms} + \mathrm{prompt\_len} / \mathrm{prefill\_tokens\_per\_ms} \) 模拟 prefill、用常数 \( \mathrm{TPOT} \) 模拟 decode；所有 f64→`Duration` 换算都走 `duration_from_ms` 的双闸门（有限非负 + 可表示），错误上抛而非 panic。
- **`step_legacy` 的三段式**：准入循环（drain 队列、`planned_completion` 定输出、按 TTFT 预约首 token）→ 推进循环（每请求每步最多弹一个 token，吐完即终结）→ 停靠。刚准入的请求可在同一个 step 里发出首 token。
- **TPOT 从实际吐出时刻重排**（L733 取新鲜 `Instant::now()`），节拍是「发出到发出」的间隔；到期判定只认 `Instant` 比较，睡眠只是节能手段，因此间隔下界精确。
- **`RunningRequest.pending` 反转存储**：一次 `reverse()` 换取整个生命周期的 O(1) `pop()`；`finish_reason` 在准入时就已确定——legacy 车道没有运行中的不确定性。
- **`WAIT_SLICE = 1ms` 切片睡眠**：新提交只在两次 step 之间被 drain，睡满会冻结准入、抬高实际 TTFT；完全不睡则因 metrics 报告 running ≥ 1 而让驱动线程烧满一个核。1ms 是两难的折中，且被档案车道复用。
- **每 step 每请求至多一个 token** 的节拍与真实引擎的 step contract 对齐——这正是 sim 能用 CPU 验证前端行为的前提之一。

## 7. 下一步学习建议

- **下一讲 u2-l2（假想输出）**：本讲只碰了 `planned_completion` 的交界处——它返回什么，legacy 车道就发什么。下一讲深入它的全部分支（脚本重放、`Stop` vs `Length` 的判定）以及 `logprobs.rs` 如何合成 `PromptEcho` 与 `TokenLogprob` 协议夹具。
- **之后再进 u2-l3/u2-l4（worker 状态机）**：对比「输出与时间从一开始就完全确定」的 legacy 车道，与「先规划再推进」的确定性 vLLM V1 式调度——你会看到 `plan_step`/`complete_step` 把本讲隐式的时间线拆成了显式的两相。
- **回看 u1-l3 的 `start_engine_with_partitions`**：现在你应该能解释为什么每个分区克隆一份配置、各跑一个独立的 `SimScheduler`——`queued`/`running` 都是每分区私有状态，车道之间互不可见。
- 建议顺手重读 [driver.rs 的 `drive` 循环](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/driver.rs#L59-L104)一遍：本讲所有「为什么」的答案（谁调 step、何时 drain、何时 commit、何时自旋）都写在那 45 行里。
