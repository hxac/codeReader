# 诊断处理：去重、延迟错误与友好提示

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `typst` crate 里**两套**诊断去重机制分别在「什么时候、对谁、用什么键」起作用：`Sink::warn` 在编译过程中对**告警**就地去重，`deduplicate` 在编译失败时对**错误**整批去重。
- 解释「延迟错误（delayed error）」的语义：为什么 show 规则在内省循环的早轮抛出的错误**不该**立刻致命，以及它们如何被「先攒后判、末轮仍存在才提升为致命错误」。
- 读懂 `hint_invalid_main_file` 如何在主文件读取失败时，针对 UTF-8 解码失败给出「你是不是把 `.typ` 写成了别的扩展名」的友好提示。
- 理解 `warn_or_error_for_html` / `warn_or_error_for_bundle` 如何根据 `Feature` 开关，对同一份 HTML / bundle 编译目标在「告警」与「报错」之间二选一。

本讲是 **advanced** 层。默认你已经学过 u2-l1（`compile_impl` 七阶段主流程）和 u2-l4（`Engine` / `Sink` / `Route` / `Traced` 四个上下文对象）。本讲要回答的核心问题是：**`compile` 周边的诊断，是怎样被「去重、延迟、提示、门控」打磨成用户最终看到的那一份干净报错的？**

## 2. 前置知识

先用通俗语言把几个概念讲清楚。

- **诊断（diagnostic）**：编译器向用户汇报的一条信息。在 Typst 里它就是 `SourceDiagnostic` 结构体，含 5 个字段：`severity`（`Error` 或 `Warning`）、`span`（出错位置）、`message`（主消息）、`trace`（调用栈）、`hints`（附加建议）。**错误**会终止编译、进入 `SourceResult::Err`；**告警**不终止编译，单独收集进 `Sink`。详见 [crates/typst-library/src/diag.rs:L297-L321](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L297-L321)。
- **错误是「一批」而不是「一个」**：`SourceResult<T>` 的失败类型是 `EcoVec<SourceDiagnostic>`（[diag.rs:L210](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L210)），这样编译器可以一次性汇报多个错误，而不是碰一个停一个。
- **内省循环（introspection loop）**：u2-l2 讲过，`query`、计数器、目录页码等「内省」依赖最终布局，而布局又受内省影响，构成不动点迭代，所以 `compile_impl` 会反复布局至多 `MAX_ITERS = 5` 轮直到内省稳定。本讲的「延迟错误」正是为这个循环服务的。
- **特性开关（feature flag）**：HTML、bundle 等仍是实验能力，被收敛成 `Feature` 枚举，构建 `Library` 时显式开启才可用（u3-l3 讲过装配，u3-l1 讲过 `Target`）。
- **`Sink`**：u2-l4 讲过的「只增容器」，贯穿求值与布局，收集告警、延迟错误、内省记录、追踪值。本讲会反复回到它的字段定义。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | 本讲主战场：`compile` 的去重重挂点、`compile_impl` 的目标门控 / 主文件提示 / 末轮提升、以及 `deduplicate` / `hint_invalid_main_file` / `warn_or_error_for_html` / `warn_or_error_for_bundle` 四个辅助函数全部在这里。 |
| [crates/typst-library/src/engine.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs) | `Sink` 的字段（`delayed` / `warnings` / `warnings_set`）、`Sink::warn` 的就地去重、`Sink::delayed()` 的取出、`Engine::delay` 的「吞错续跑」。 |
| [crates/typst-library/src/diag.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs) | `SourceDiagnostic` / `Severity` / `FileError::InvalidUtf8` 的定义，`bail!` / `warning!` 宏，以及 `hint()` 方法。 |
| [crates/typst-library/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs) | `Features` 位集合与 `Feature` 枚举、`is_enabled` 方法——门控告警的判定依据。 |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) | show 规则归约中调用 `engine.delay(...)` 的真实位置，是「延迟错误」最典型的生产者。 |

> 一条贯穿全讲的线索：诊断从「产生」到「被用户看到」要经过四道工序——**门控**（4.4，编译一开始就决定能不能走这条路）→ **延迟**（4.2，循环中先攒着不致命）→ **末轮提升**（4.2，循环结束后才判定致命）→ **去重**（4.1，最后整批去重）。`hint_invalid_main_file`（4.3）则是另一条「读主文件失败」的快捷致命路径，附带友好提示。

---

## 4. 核心概念与源码讲解

### 4.1 诊断去重：`deduplicate` 与 `Sink::warn` 的双轨制

#### 4.1.1 概念说明

Typst 编译会**反复**或**并行**地求值同一片代码：

- 内省循环最多把布局跑 5 轮；
- `Engine::parallelize` 会用 rayon 把一批子任务（比如一页里的多个元素）丢到不同线程并行算，每个子任务各带一个临时 `Sink`。

于是同一条错误/告警很可能被产生很多次。如果原样汇报，用户会被「同一条报错刷屏」淹没。所以编译器必须**去重**：位置（`span`）和消息（`message`）都相同的诊断，只保留第一份。

关键在于：Typst 用了**两套时机不同的去重**，且用的是**同一个哈希键**——

| 机制 | 作用对象 | 时机 | 键 |
| --- | --- | --- | --- |
| `Sink::warn` | 告警（`Severity::Warning`） | 写入 `Sink` 的那一刻就地去重 | `hash128(&(&span, &message))` |
| `deduplicate` | 错误（`Severity::Error`） | 编译失败时，对最终错误整批去重 | `hash128(&(&span, &message))` |

为什么错误不能像告警那样就地去重？因为错误（尤其是「延迟错误」，见 4.2）在 `Sink` 里是**原样累加、不做去重**的，要等到循环结束、提升为致命错误之后，再统一去重一次。这两套机制的分工，正是本节要讲清的设计。

#### 4.1.2 核心流程

去重的判定可以写成一个集合成员判定：

\[ \text{保留该诊断} \iff k \notin U,\quad \text{其中 } k = H(\text{span},\ \text{message}),\quad U \leftarrow U \cup \{k\} \]

即每来一条诊断，算它的 128 位哈希键 \(k\)，若 \(k\) 已在「已见集合」\(U\) 里就丢弃，否则保留并把 \(k\) 登记进 \(U\)。

- 对**告警**，这个判定发生在 `Sink::warn` 内部，\(U\) 就是 `Sink.warnings_set`，伴随整个编译过程持续维护。
- 对**错误**，这个判定发生在编译的最末尾 `deduplicate` 里，\(U\) 是一个临时新建的集合，一次性处理整批错误。

#### 4.1.3 源码精读

先看 `compile` 怎么挂上 `deduplicate`——它**只**挂在错误路径上（`.map_err`），告警路径不经过它：

```rust
let output = compile_impl::<T>(world.track(), Traced::default().track(), &mut sink)
    .map_err(deduplicate);                       // ← 只对 Err 起作用
Warned { output, warnings: sink.warnings() }     // ← 告警另走 sink.warnings()
```

完整代码见 [crates/typst/src/lib.rs:L78-L82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L78-L82)。注意 `warnings` 来自 `sink.warnings()`，而 `Sink::warn` 在写入时已经去过重了，所以告警到这一步已是干净的。

再看 `deduplicate` 本体——用 `retain` + 一个 `FxHashSet` 保留每条错误首次出现：

```rust
fn deduplicate(mut diags: EcoVec<SourceDiagnostic>) -> EcoVec<SourceDiagnostic> {
    let mut unique = FxHashSet::default();
    diags.retain(|diag| {
        let hash = typst_utils::hash128(&(&diag.span, &diag.message));
        unique.insert(hash)          // insert 返回 false（已存在）则被 retain 丢弃
    });
    diags
}
```

见 [crates/typst/src/lib.rs:L197-L204](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L197-L204)。`FxHashSet::insert` 在键已存在时返回 `false`，`Vec::retain` 据此把重复项过滤掉。

对照 `Sink::warn`，键完全一样、只是就地维护 `warnings_set`：

```rust
pub fn warn(&mut self, warning: SourceDiagnostic) {
    let hash = typst_utils::hash128(&(&warning.span, &warning.message));
    if self.warnings_set.insert(hash) {
        self.warnings.push(warning);             // 仅当首次出现才真正存下
    }
}
```

见 [crates/typst-library/src/engine.rs:L221-L228](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L221-L228)，配套字段 `warnings_set` 见 [engine.rs:L162-L164](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L162-L164)。

最后看一个关键细节，解释了「为什么错误需要末尾再去重」：`Sink` 在合并子 sink 时，**告警走 `warn`（去重），延迟错误走 `extend`（不去重）**：

```rust
self.delayed.extend(delayed);          // ← 原样累加，不去重
for warning in warnings {
    self.warn(warning);                // ← 逐条去重
}
```

见 [crates/typst-library/src/engine.rs:L245-L249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L245-L249)。`Engine::parallelize` 合并各并行子任务时也是走这条 `extend`（[engine.rs:L91-L99](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L91-L99)）。所以 N 个并行分支若产生同一条延迟错误，主 sink 里就会有 N 份——这正是 `deduplicate` 要在最后兜底清理的对象。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**：

1. **实践目标**：确认两套去重用同一个键、且职责不重叠。
2. **操作步骤**：
   - 打开 [crates/typst/src/lib.rs:L200](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L200) 和 [crates/typst-library/src/engine.rs:L224](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L224)，比对两处的 `typst_utils::hash128(&(&..., &...))` 调用，确认键都是 `(span, message)` 的二元组。
   - 打开 [crates/typst-library/src/engine.rs:L238-L253](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L238-L253) 的 `Sink::extend`，确认 `delayed` 走 `extend`、`warnings` 走 `warn`。
   - 思考：假如把 `deduplicate` 从 `compile` 里删掉，哪一类诊断会重复出现？（答案：错误，尤其是并行/循环产生的延迟错误。）
3. **需要观察的现象**：两处哈希键字面一致；`extend` 对两类诊断的处理方式不同。
4. **预期结果**：你会得出「告警就地去重、错误末尾整批去重，键相同」的结论。
5. 运行结果：**待本地验证**（若想实证，可在本地构造一个会触发多条相同错误的 Typst 源文件，对比有无 `deduplicate` 时 CLI 的输出条数）。

#### 4.1.5 小练习与答案

1. **问**：`deduplicate` 为什么用 `.map_err(...)` 挂载，而不是在 `Sink` 里像告警那样就地维护？
   **答**：因为 `Sink` 对延迟错误是「原样累加、不去重」（`delayed.extend`），且这些错误要等到循环结束、提升为致命错误后才组成最终的 `Err`。在末尾用 `deduplicate` 一次性整批处理，既简单又能在「并行子 sink 合并」「循环收敛轮合并」两个引入重复的关口之后统一兜底。

2. **问**：两条 `message` 相同但 `span` 不同的错误，会被 `deduplicate` 视为重复吗？
   **答**：不会。键是 `(span, message)` 二元组，只要 `span` 不同，哈希就不同，两条都保留。这与「同一段代码被求值多次产生完全相同的诊断」才是重复的语义一致。

---

### 4.2 延迟错误与「末轮提升」

#### 4.2.1 概念说明

「延迟错误（delayed error）」是 Typst 诊断系统里最精巧的一笔。问题来自内省循环：一条 show 规则可能在内省稳定**之前**就抛错——比如它内部 `query(heading)` 去取标题，而前几轮布局还没定型，查询结果不完整，于是报错。但这往往只是「时候未到」的**假阳性**，等内省收敛后，同一个查询就不会再错了。

如果一遇到错就立刻终止编译，用户会被这些「过会儿就消失」的错误搞得莫名其妙。所以 Typst 的做法是：

- 遇到这类错误时，**不立刻致命**，而是把它塞进 `Sink` 的 `delayed` 桶，继续用「空内容」顶替着往下算；
- 等整个内省循环跑完，**再回头看** `delayed` 桶：如果某条错误到末轮**仍然存在**，说明它不是「时候未到」而是「真错」，这时才把它提升（promote）为致命错误。

一句话：**延迟错误 = 先攒后判，末轮仍在才致命。**

#### 4.2.2 核心流程

延迟错误的完整生命周期（结合 u2-l1 的七阶段与 u2-l2 的循环）：

```
内省循环的某一轮（收敛轮）内
   show 规则求值 ──出错──► Engine::delay(result)
                                   ├─ Ok(v)  ► 用 v 继续
                                   └─ Err(e) ► e 存入本轮 subsink.delayed
                                              ► 续跑，用 T::default()（通常是空内容）顶替
   本轮 subsink 经 sink.extend_from_sink(subsink) 合并进主 sink
                                   （只有收敛轮 / 末轮的 subsink 才合并；早轮的 subsink 被丢弃）
循环结束后
   let delayed = sink.delayed();          // 取出主 sink 里攒下的延迟错误
   if !delayed.is_empty() { return Err(delayed); }   // 末轮仍在 ► 提升为致命
   否则 ► Ok(document)
```

有两点特别关键，都和 u2-l2 呼应：

1. **早轮的延迟错误天然被丢弃**：循环里每轮都用一个全新的 `subsink`（[lib.rs:L146](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L146)），只有收敛那一轮（或 5 轮不收敛时的末轮）的 `subsink` 才通过 `sink.extend_from_sink(subsink)` 合并进主 sink（[lib.rs:L159](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L159)、[lib.rs:L177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L177)）。所以「早轮才出现、末轮已消失」的假阳性根本进不了主 sink，连「提升」的资格都没有。
2. **求值阶段直接写主 sink**：循环之前的 `typst_eval::eval(...)` 拿到的是主 `sink` 本身（[lib.rs:L127](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L127)），求值期间产生的延迟错误直接进主 sink，不受循环 subsink 隔离影响——这是合理的，因为它们与具体哪一轮布局无关。

#### 4.2.3 源码精读

「吞错续跑」的实现是 `Engine::delay`，它把一个 `SourceResult<T>` 变成 `T`：

```rust
pub fn delay<T: Default>(&mut self, result: SourceResult<T>) -> T {
    match result {
        Ok(value) => value,
        Err(errors) => {
            self.sink.delayed_errors(errors);   // 攒进 delayed 桶，不致命
            T::default()                         // 用默认值（通常空内容）顶替续跑
        }
    }
}
```

见 [crates/typst-library/src/engine.rs:L42-L50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L42-L50)。注意它要求 `T: Default`——出错时必须能拿出一个「零值」来顶替，对内容元素而言就是空内容。

最典型的生产者是 show 规则归约。在 `typst-realize` 里，应用一条 show 规则的结果用 `engine.delay(...)` 包裹，源码注释把意图说得非常直白：

```rust
// Errors in show rules don't terminate compilation immediately. We just
// continue with empty content for them and show all errors together, if
// they remain by the end of the introspection loop.
//
// This way, we can ignore errors that only occur in earlier iterations
// and also show more useful errors at once.
output = Cow::Owned(s.engine.delay(result));
```

见 [crates/typst-realize/src/lib.rs:L396-L402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L396-L402)。这段注释正是「延迟错误」语义的最佳注脚：show 规则出错时先用空内容顶替、把错误攒起来，循环结束仍存在才一并报出。

「末轮提升」则发生在 `compile_impl` 的循环之后——这是延迟错误的最终审判点：

```rust
// Promote delayed errors.
let delayed = sink.delayed();
if !delayed.is_empty() {
    return Err(delayed);
}

Ok(document)
```

见 [crates/typst/src/lib.rs:L187-L193](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L187-L193)。`sink.delayed()` 用 `std::mem::take` 把桶掏空（[engine.rs:L183-L186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L183-L186)）；非空就直接 `return Err(delayed)`，这个 `Err` 一路回到 `compile`，再被 4.1 的 `deduplicate` 去重。`Sink.delayed` 字段及其设计意图见 [engine.rs:L155-L160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L155-L160)。

#### 4.2.4 代码实践

**源码阅读 + 场景推演型实践**：

1. **实践目标**：把「延迟错误从产生到提升」的链路在源码里走一遍，并用一个具体场景推演它在前几轮被吞、末轮被报的过程。
2. **操作步骤**：
   - 从 [crates/typst-realize/src/lib.rs:L402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L402) 的 `s.engine.delay(result)` 出发，跳到 [engine.rs:L42-L50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L42-L50) 看 `delay`，再跳到 [lib.rs:L187-L191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L187-L191) 看提升，画出这条「产生 → 暂存 → 末轮提升」的链路图。
   - 设想一条 show 规则 `#show heading: it => query(heading).len()`：在第 1 轮，布局未定型，`query` 的结果可能让规则出错 → 被 `delay` 吞掉、用空内容顶替；假设第 2 轮内省收敛，规则在第 2 轮不再出错。因为只有收敛轮（第 2 轮）的 `subsink` 才合并进主 sink，而第 2 轮没有产生延迟错误，所以主 sink 的 `delayed` 桶是空的 → `sink.delayed()` 返回空 → 编译成功，用户**看不到**那个第 1 轮的假阳性错误。
3. **需要观察的现象**：早轮的 `subsink` 与主 `sink` 是分离的；只有末轮才合并。
4. **预期结果**：理解「末轮提升」真正判定的不是「曾经出错」，而是「收敛轮仍然出错」。
5. 运行结果：**待本地验证**。

#### 4.2.5 小练习与答案

1. **问**：`Engine::delay` 为什么要求 `T: Default`？
   **答**：因为出错时要继续往下编译，必须拿一个「零值」顶替原本应得的值（对内容元素就是空内容）。`T::default()` 提供这个零值，保证后续流程不被一个 `Err` 打断。

2. **问**：如果一条 show 规则的错误在第 1～4 轮都出现、第 5 轮（收敛轮）不再出现，用户最终会看到这条错误吗？
   **答**：不会。前 4 轮的错误都留在各自被丢弃的 `subsink` 里，只有第 5 轮的 `subsink` 合并进主 sink；第 5 轮没有这条错误，主 sink 的 `delayed` 桶为空，`sink.delayed()` 返回空，提升点 `return Err(delayed)` 不会触发。

3. **问**：`sink.delayed()` 为什么用 `std::mem::take` 而不是返回引用？
   **答**：`delay` 标注在 `#[comemo::track]` 的 `Sink` 上，调用方拿到的是 `&mut self`（其实是 `TrackedMut`），无法把对内部字段的引用交还给外部所有权世界；`mem::take` 把桶内容 move 出来、留下空桶，是最简单稳妥的取出方式。

---

### 4.3 主文件读取失败：`hint_invalid_main_file` 的友好提示

#### 4.3.1 概念说明

`compile_impl` 一开始要做一件事：把主源文件（`world.main()` 指向的那个 `FileId`）读出来变成 `Source`。这一步若失败，编译无法开始，属于**致命快捷路径**——直接 `?` 返回错误。

但「读主文件失败」本身的消息（如 `file is not valid UTF-8`）对用户很不友好。最常见的真实场景是：用户把入口文件名误填成了 `report.pdf`（一个二进制 PDF）而不是 `report.typ`，于是 Typst 把 PDF 当文本读、UTF-8 解码失败。`hint_invalid_main_file` 就是为这类场景兜底的「友好提示生成器」：它在原始错误之上**追加 hints**，引导用户「你是不是把扩展名写错了」。

#### 4.3.2 核心流程

```
world.source(main) 返回 Err(file_error)
   └─ map_err(|err| hint_invalid_main_file(world, err, main))
         ├─ 构造基础错误：SourceDiagnostic::error(Span::detached(), file_error)
         ├─ 仅当 file_error 是 InvalidUtf8 时考虑加 hint：
         │     ├─ 扩展名是 "typ"  ► 不加任何 hint（文件确实是坏的，别误导）
         │     ├─ 有别的扩展名   ► hint:「.xxx 一般不是 Typst 文件」
         │     └─ 无扩展名       ► hint:「无扩展名一般不是 Typst 文件」
         ├─ 若把扩展名换成 "typ" 后能成功读到源码 ► 追加 hint:「检查是否该用 .typ」
         └─ 返回 eco_vec![diagnostic]
   ?  ► 致命返回（这个 EcoVec 成为 compile 的 Err）
```

只对 `FileError::InvalidUtf8` 加提示（其它失败如「文件不存在」不加），因为 UTF-8 失败最可能源自「把二进制文件当成了 Typst 源码」这种扩展名误用。

#### 4.3.3 源码精读

调用点在 `compile_impl` 取主源码处：

```rust
let main = world.main();
let main = world
    .source(main)
    .map_err(|err| hint_invalid_main_file(world, err, main))?;
```

见 [crates/typst/src/lib.rs:L117-L120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L117-L120)。`?` 意味着返回的 `EcoVec<SourceDiagnostic>` 直接成为本次编译的致命错误。

`hint_invalid_main_file` 的核心判定：

```rust
let is_utf8_error = matches!(file_error, FileError::InvalidUtf8);
let mut diagnostic =
    SourceDiagnostic::error(Span::detached(), EcoString::from(file_error));

if is_utf8_error {
    match input.vpath().extension() {
        Some("typ") => return eco_vec![diagnostic],     // 已经是 .typ，确实是坏文件，不加 hint
        Some(ext) => {
            diagnostic.hint(eco_format!(
                "a file with the `.{ext}` extension is not usually a Typst file",
            ));
        }
        None => {
            diagnostic.hint("a file without an extension is not usually a Typst file");
        }
    }

    if world.source(input.map(|p| p.with_extension("typ")).intern()).is_ok() {
        diagnostic.hint("check if you meant to use the `.typ` extension instead");
    }
}

eco_vec![diagnostic]
```

见 [crates/typst/src/lib.rs:L208-L244](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L208-L244)。逐段理解：

- `SourceDiagnostic::error(Span::detached(), ...)`：用 `Span::detached()`（无具体源码位置）创建错误，消息直接是 `file_error` 的 `Display` 结果——对 `InvalidUtf8` 就是 `"file is not valid UTF-8"`（见 [diag.rs:L655](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L655)）。
- `Some("typ") => return`：如果主文件本来就是 `.typ`，那它确实就是坏文件，不该误导用户去查扩展名，直接返回裸诊断。
- `Some(ext)` / `None`：给出「该扩展名/无扩展名通常不是 Typst 文件」的提示。
- 最贴心的一步：`world.source(... .with_extension("typ") ...).is_ok()` ——尝试把扩展名换成 `.typ` 再读一次，如果**真能读到**，就追加「检查是否该用 `.typ` 扩展名」。这正是「`report.pdf` 旁边其实有个 `report.typ`」的场景。

`diagnostic.hint(...)` 把一条 `Spanned::detached` 的提示 push 进 `SourceDiagnostic.hints` 字段（[diag.rs:L355-L358](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L355-L358)，字段定义 [diag.rs:L313-L320](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L313-L320)）。CLI 渲染时这些 hint 会以 `hint: ...` 列在错误下方。

#### 4.3.4 代码实践

**分支推演型实践**：

1. **实践目标**：把 `hint_invalid_main_file` 的三个分支（`.typ` / 其它扩展名 / 无扩展名）以及「换 `.typ` 重试」的逻辑在脑中跑一遍，预测每种情况下用户看到的 hints。
2. **操作步骤**：打开 [crates/typst/src/lib.rs:L220-L241](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L220-L241)，对下表的三种「误填入口文件名」场景，分别写出 `match` 走哪一臂、追加哪些 hint：

   | 用户填的主文件 | `extension()` 命中 | 是否换 `.typ` 重试成功 | 最终 hints |
   | --- | --- | --- | --- |
   | `report.pdf`（旁边有 `report.typ`） | ? | ? | ? |
   | `report.pdf`（旁边**没有** `report.typ`） | ? | ? | ? |
   | `report.typ`（文件确实是损坏的二进制） | ? | ? | ? |

3. **需要观察的现象**：`.typ` 分支直接 return，不走换扩展名重试；非 `.typ` 分支才会尝试换 `.typ`。
4. **预期结果**：第一行得到「.pdf 一般不是 Typst 文件」+「检查是否该用 .typ」两条 hint；第二行只有第一条；第三行（`.typ`）零 hint。
5. 运行结果：**待本地验证**。

#### 4.3.5 小练习与答案

1. **问**：为什么 `hint_invalid_main_file` 只在 `FileError::InvalidUtf8` 时加提示，对 `FileError::NotFound` 不加？
   **答**：「文件不存在」的成因很多（路径错、权限、包未下载等），很难给出靠谱的扩展名建议；而 UTF-8 解码失败几乎总是「把一个二进制 / 非 Typst 文件当成了源码入口」，扩展名误用是头号嫌疑，所以值得专门提示。

2. **问**：`hint_invalid_main_file` 产生的错误，会经过 4.1 的 `deduplicate` 吗？
   **答**：会。它返回的 `EcoVec<SourceDiagnostic>` 经 `?` 成为 `compile_impl` 的 `Err`，再成为 `compile` 的 `Err`，最后被 `.map_err(deduplicate)` 处理。只不过这条路径通常只产生一条诊断，去重与否结果相同。

---

### 4.4 目标门控告警：`warn_or_error_for_html` / `warn_or_error_for_bundle`

#### 4.4.1 概念说明

`compile<T: Output>` 能产出多种目标（u3-l1 讲过 `Target` 枚举：`Paged` / `Html` / `Bundle`）。其中 HTML 和 bundle 仍是**实验特性**。Typst 对实验特性的态度是：

- 如果你**没有**显式开启对应的 `Feature`，却请求了这个目标，那是「你没资格用」——直接**报错**，编译失败；
- 如果你**已经**显式开启了对应 `Feature`，说明你知情同意，但 Typst 仍要**提醒**你「这功能没完工、随时会变」——发一条**告警**，编译继续。

所以同一个函数，依据 `Feature` 开关在「告警」与「报错」之间二选一——这就是「特性门控告警」。`Paged` 是稳定目标，无需门控，`compile_impl` 的 `match` 对它走空臂。

#### 4.4.2 核心流程

```
compile_impl 一开始
   match T::target()
     Paged  ► {} （空臂，直接放行）
     Html   ► warn_or_error_for_html(&library.features, sink)?
     Bundle ► warn_or_error_for_bundle(&library.features, sink)?

warn_or_error_for_X(features, sink):
   if features.is_enabled(Feature::X):
       sink.warn(warning!(...))      ► 告警进 sink，返回 Ok(())，编译继续
   else:
       bail!(...)                    ► 返回 Err，经 ? 立刻致命（连主文件都还没读）
```

关键点：`?` 让「未开启特性」的情况在 `compile_impl` **最开始**就失败返回，连取主源码、求值、布局统统不执行——典型的「快速失败（fail fast）」。而告警分支把信息写进主 `sink`（注意此时是主 sink，不是循环里的 subsink），随后照常编译。

#### 4.4.3 源码精读

门控发生在 `compile_impl` 的第一段：

```rust
let library = world.library();
match T::target() {
    Target::Paged => {}
    Target::Html => warn_or_error_for_html(&library.features, sink)?,
    Target::Bundle => warn_or_error_for_bundle(&library.features, sink)?,
}
```

见 [crates/typst/src/lib.rs:L104-L109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L104-L109)。`library.features` 就是 u3-l3 讲过、随身存在 `Library` 里的特性开关备忘。

`warn_or_error_for_html` 的完整逻辑（bundle 同构）：

```rust
fn warn_or_error_for_html(features: &Features, sink: &mut Sink) -> SourceResult<()> {
    const ISSUE: &str = "https://github.com/typst/typst/issues/5512";
    if features.is_enabled(Feature::Html) {
        sink.warn(warning!(
            Span::detached(),
            "html export is under active development and incomplete";
            hint: "its behaviour may change at any time";
            hint: "do not rely on this feature for production use cases";
            hint: "see {ISSUE} for more information";
        ));
    } else {
        bail!(
            Span::detached(),
            "html export is only available when `--features html` is passed";
            hint: "html export is under active development and incomplete";
            hint: "see {ISSUE} for more information";
        );
    }
    Ok(())
}
```

见 [crates/typst/src/lib.rs:L247-L266](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L247-L266)（bundle 版本见 [lib.rs:L270-L286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L270-L286)）。注意几个细节：

- `features.is_enabled(Feature::Html)`：`Features` 是个 `SmallBitSet`，`is_enabled` 检查对应比特位（[typst-library/src/lib.rs:L254-L257](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L254-L257)）；`Feature` 枚举有 `Html` / `Bundle` / `A11yExtras` 三项（[lib.rs:L273-L277](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L273-L277)）。
- 开启时用 `warning!` 宏构造一条 `Severity::Warning` 诊断，经 `sink.warn` 入桶（顺带被 4.1 的就地去重保护）。
- 未开启时用 `bail!` 宏（[diag.rs:L49-L76](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L49-L76)）early-return 一条 `Span::detached()` 的错误。`Span` 同样是 detached，因为特性门控失败不指向任何源码位置。
- 即使开启了特性也要告警——这是给「知情用户」的持续提醒：别拿它上生产。

#### 4.4.4 代码实践

**对照阅读型实践**：

1. **实践目标**：看清「同一目标、两种 feature 状态」下用户得到的是告警还是致命错误，并理解 `?` 带来的「快速失败」。
2. **操作步骤**：
   - 打开 [crates/typst/src/lib.rs:L104-L109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L104-L109)，确认 `Target::Paged` 走空臂、`Html`/`Bundle` 走门控函数。
   - 打开 [crates/typst/src/lib.rs:L247-L266](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L247-L266)，回答：调用 `compile::<HtmlDocument>(&world)` 时，若 `Feature::Html` 未开启，`compile_impl` 会执行到哪一行就返回？后面的取主源码、求值、循环还会跑吗？
3. **需要观察的现象**：`bail!` 在 `else` 分支里、`?` 紧跟函数调用。
4. **预期结果**：未开启时 `bail!` 让 `warn_or_error_for_html` 返回 `Err`，`?` 把它直接变成 `compile_impl` 的返回值，编译在门控阶段就结束，后续阶段都不执行。
5. 运行结果：**待本地验证**。

#### 4.4.5 小练习与答案

1. **问**：开启 `Feature::Html` 后，每次 HTML 编译都会看到那条「under active development」告警吗？能不能去掉？
   **答**：会。只要走 `Target::Html` 且 `features.is_enabled(Feature::Html)` 为真，`sink.warn(...)` 就会执行，告警一定会进 `sink`。这是设计上有意为之的「持续提醒」，无法由用户在脚本层关闭（除非改 `Library` 的 features 配置）。

2. **问**：`warn_or_error_for_html` 里的告警和报错都用 `Span::detached()`，为什么？
   **答**：特性门控的失败/提醒是「编译目标级别」的问题，不对应任何一段源码，没有合适的 `Span` 可指，所以用 detached（空）span。CLI 会把它当作「无位置的全局诊断」展示。

---

## 5. 综合实践

本练习把 4.1 与 4.2 串起来，要求你**用文字构造两个场景**，分别触发「重复诊断」和「延迟错误」，并说明两道工序各起什么作用、用户最终看到什么。这也是本讲规格指定的实践任务。

### 场景 A：重复诊断（检验 `deduplicate`）

**触发条件**（文字描述即可）：一份 Typst 文档里，某一页用 `grid` 布局了 20 个单元格，每个单元格内都引用了同一个未定义的标识符（或同一条会抛错的 show 规则）。`Engine::parallelize` 把这 20 个单元格丢到多个线程并行归约，每个线程在自己的临时 `Sink` 里各产生一条**完全相同**（`span` 与 `message` 都一致）的错误。这些临时 `Sink` 经 `extend` 合并回主 `sink` 时，`delayed` 走 `extend`（不去重），于是主 `sink.delayed` 里攒了 20 份相同的错误。

- **`deduplicate` 的作用**：循环结束后这 20 份错误被 `sink.delayed()` 取出、`return Err(delayed)` 提升为致命错误，再经 `compile` 的 `.map_err(deduplicate)` 用 `(span, message)` 哈希去重，20 份合并成 1 份。
- **末轮提升的作用**：本场景不涉及「早轮消失」，提升点只是把延迟错误「转正」为致命错误，本身不去重。
- **用户最终看到的诊断**：**一条**错误（而非 20 条刷屏），定位到那个未定义标识符。

### 场景 B：延迟错误（检验「末轮提升」）

**触发条件**：一条 show 规则在内省稳定**之前**会抛错（例如 `#show heading: it => { query(heading); panic("x") }` 这类依赖未定型布局的逻辑），但在收敛轮不再抛错。

- **末轮提升的作用**：前几轮的错误留在各自被丢弃的 `subsink` 里，只有收敛轮的 `subsink` 合并进主 `sink`；收敛轮没有这条错误，主 `sink.delayed` 为空，`sink.delayed()` 返回空，提升点 `return Err(delayed)` 不触发。
- **`deduplicate` 的作用**：本场景主 `sink.delayed` 为空，根本不会走到 `return Err(delayed)`，`deduplicate` 无事可做。
- **用户最终看到的诊断**：**零**条该错误（编译成功，假阳性被吞掉）。

> 把两个场景对照看：`deduplicate` 解决的是「同一条错误被重复产生」，末轮提升解决的是「临时错误不该致命」。它们一个做「量」的收敛、一个做「时」的收敛，共同把内省循环 + 并行求值产生的噪声，打磨成一份干净、准确的最终报错。

若想本地实证，可在 typst 仓库根目录用 `cargo build --release` 编出 CLI，分别构造上述两份 `.typ` 源文件运行 `typst compile`，观察输出条数与是否报错。**待本地验证**。

## 6. 本讲小结

- Typst 有**两套**用同一个键 `hash128(&(span, message))` 的去重机制：`Sink::warn` 对**告警**在写入时就地去重，`deduplicate` 对**错误**在编译失败的末尾整批去重（[lib.rs:L80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L80)、[engine.rs:L221-L228](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L221-L228)）。
- 错误之所以要末尾整批去重，是因为 `Sink` 对**延迟错误**只做 `extend` 原样累加、不就地去重，并行子任务与循环收敛轮合并都会引入重复（[engine.rs:L245-L249](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L245-L249)）。
- 「延迟错误」语义：show 规则等在内省早轮抛出的假阳性错误，经 `Engine::delay` 暂存、用空内容顶替续跑，**只有收敛轮仍存在**才在循环后提升为致命错误（[engine.rs:L42-L50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L42-L50)、[realize/lib.rs:L396-L402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L396-L402)、[lib.rs:L187-L193](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L187-L193)）。
- `hint_invalid_main_file` 在主文件 UTF-8 解码失败时，针对 `.typ` / 其它扩展名 / 无扩展名三种情况给出不同 hints，还会尝试换 `.typ` 重读以判断「是否扩展名写错」（[lib.rs:L208-L244](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L208-L244)）。
- `warn_or_error_for_html` / `warn_or_error_for_bundle` 依据 `Feature` 开关对实验目标在「告警」与「报错」间二选一：开启则提醒、未开启则 `bail!` 快速失败（[lib.rs:L104-L109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L104-L109)、[lib.rs:L247-L286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L247-L286)）。
- 一条诊断从产生到被用户看到，要依次经过：**门控**（4.4）→ **延迟**（4.2 循环中）→ **末轮提升**（4.2 循环后）→ **去重**（4.1）；`hint_invalid_main_file`（4.3）则是读主文件失败这条独立快捷致命路径上的友好提示。

## 7. 下一步学习建议

- 本单元（u3）剩余最后一篇 **u3-l5 `trace()` 值追踪机制**，它和 `compile` 共用 `compile_impl`，但关注的是 `Sink.values()` 与 `Traced`。学完它，你就能把 `Sink` 的四个桶（`introspections` / `delayed` / `warnings` / `values`）全部串起来——本讲讲了 `delayed` 和 `warnings`，u3-l5 补上 `values`，u2-l3 补上了 `introspections`。
- 想再看真实诊断如何被**渲染**给用户，可读 `typst-cli` 里把 `SourceDiagnostic` 转成终端彩色输出的代码（搜索 `SourceDiagnostic` 在 `crates/typst-cli` 下的引用），体会 `hints` / `trace` / `severity` 如何变成屏幕上的文本。
- 想深入「延迟错误」的源头，可顺着 [crates/typst-realize/src/lib.rs:L402](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L402) 进入 `typst-realize` crate，通读 show 规则归约的主循环，理解哪些错误被设计成「可延迟」、哪些是立即致命。
