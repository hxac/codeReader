# 集成实践：typst-kit Timer 与端到端计时导出

## 1. 本讲目标

前面九讲我们都在 typst-timing 这个「基础设施」crate 内部打转：知道了事件怎么产生（u1-l2、u2-l1）、存在哪里（u2-l2）、时间怎么取（u2-l3）、怎么导出成 Chrome Trace JSON（u2-l4）、宏怎么埋点（u3-l2）、默认关闭时为何几乎零成本（u3-l1）、wasm 下怎么计时（u3-l3）。但有一个关键问题始终悬而未决：**真实代码里，究竟是谁在调用 `enable()` / `clear()` / `export_json()`，把它们串成一次完整的「记录一次编译」？**

答案就是本讲的主角——typst-kit 里的 `Timer`。它是一个不到 120 行的薄封装，却是 typst-timing 唯一的「真实用户」。学完本讲你应当能够：

1. 说清 `Timer` 的三种构造 `new` / `placeholder` / `new_or_placeholder` 各自的语义，以及它们在 `enable()` 上的关键差别。
2. 画出一次 `record` 调用的完整时间线：**何时 `enable`、何时 `clear`、何时执行被计时函数 `f`、何时写文件并 `export_json`**，并解释路径中 `{n}` 占位符的编号规则。
3. 理解 `resolve_span` 如何借助 `World` 把一个裸 `NonZeroU64` 还原成人类可读的 `(file, line)`，从而补上 typst-timing「不能依赖 typst-syntax」所欠下的那笔债。

本讲是整个手册的收尾，也是把前九讲的知识一次性串起来的综合练兵场。

## 2. 前置知识

本讲会大量复用前面讲过的结论，这里只做最小回顾，不展开细节：

- **全局状态（u2-l2）**：`EVENTS` 是进程级 `Mutex<Vec<Event>>`，所有线程共享、跨 `record` 累积，**不会自动清空**。这一点直接决定了 `record` 开头必须 `clear()`。
- **零成本门控（u3-l1）**：`TimingScope::with_span` 返回 `Option<Self>`，`is_enabled()` 为假时直接返回 `None`，既不加锁也不分配。所以「不 `enable()`」就等于「所有 `timed!` 形同虚设」。
- **导出门面（u2-l4）**：`export_json(writer, source)` 把私有 `Event` 序列化为 Chrome Trace JSON；其中的 `source: FnMut(NonZeroU64) -> (String, u32)` 闭包负责把裸 span 翻译成 `(file, line)`。本讲会看到这个闭包的真实实现就是 `resolve_span`。
- **裸 span 的来由（u3-l2）**：`Event.span` 是 `Option<NonZeroU64>` 而非 `typst_syntax::Span`，是为了切断 `typst-syntax → typst-timing` 的循环依赖。
- **`World` trait**：Typst 编译器对外要求的「环境」接口，提供 `source(id)`、`file(id)` 等加载方法。它是 `resolve_span` 还原 span 的唯一桥梁。如果你还没读过它，本讲 4.3 会带你看用到的两个方法。

> 如果上面任何一条让你觉得陌生，建议先回到对应讲义。本讲默认这些概念已经建立。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/typst-kit/src/timer.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs) | 本讲主战场。`Timer` 结构体、三种构造、`record` 的端到端时序、`resolve_span` 全部在这里。 |
| [crates/typst-timing/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs) | 被 `Timer` 调用的三个函数：`enable()`、`clear()`、`export_json()`。 |
| [crates/typst-syntax/src/span.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs) | `Span`/`SpanKind` 定义，`from_raw` 与 `get` 是还原 span 的入口。 |
| [crates/typst-library/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs) | `World` trait 的 `source` / `file` 方法，`resolve_span` 据此取文件内容。 |
| [crates/typst-cli/src/compile.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs) | 真实调用方之一：命令行 `typst compile --timings` 如何实例化 `Timer` 并 `record`。 |

还要注意一个前提：整个 `timer.rs` 被一行 `#![cfg(feature = "timer")]` 守住（[crates/typst-kit/src/timer.rs#L6](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L6)）——只有开启 typst-kit 的 `timer` feature 时这个模块才参与编译（[crates/typst-kit/Cargo.toml#L84-L85](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/Cargo.toml#L84-L85)）。typst-cli 在启用该 feature 后才把 `Timer` 接进编译主流程。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. `Timer` 的三种构造：`new` / `placeholder` / `new_or_placeholder`
2. `record` 的端到端时序：`enable → clear → f → export_json` 与 `{n}` 编号
3. `resolve_span`：借助 `World` 把裸 span 还原成 `(file, line)`

### 4.1 Timer 的三种构造：new / placeholder / new_or_placeholder

#### 4.1.1 概念说明

回顾前九讲，typst-timing 对外暴露的是一堆**散装原语**：`enable()` 开关、`clear()` 清空、`export_json()` 导出、`timed!` 宏埋点。这些原语本身不规定「先做什么、后做什么」。如果你手写一遍完整的计时流程，大概是这样的：

```text
1. enable()                 # 打开总开关
2. clear()                  # 清掉历史残留
3. 运行你要计时的代码 f()   # 期间遍布代码库的 timed! 自动记录
4. 把缓冲区导出成 JSON 文件  # export_json(...)
```

`Timer` 就是把这四步固化成一个可复用对象的薄封装：**「记录一次函数执行并把它写进磁盘」**。它只有两个字段：

```rust
pub struct Timer {
    path: Option<PathBuf>,  // 输出 JSON 写到哪里；None 表示「假装计时」
    iter: usize,            // 当前是第几次 record（watch 模式下递增）
}
```

见 [crates/typst-kit/src/timer.rs#L17-L22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L17-L22)。

`path` 是 `Option`，这是 `Timer` 设计的核心。它带来了三种构造方式，对应「真计时」「假计时」「按需选择」：

- **`new(path)`**：真计时。`path = Some(_)`，并且在构造时**立即** `enable()`。
- **`placeholder()`**：假计时。`path = None`，**不** `enable()`，什么也不做。
- **`new_or_placeholder(Option<PathBuf>)`**：根据传入是否为 `Some` 在前两者间二选一。

为什么要特意造一个 `placeholder()`？这是为了让**调用方代码路径统一**。无论运行时是否真的要计时，CLI 都写成同一种样子：

```rust
let mut timer = Timer::new_or_placeholder(args.timings.clone());
timer.record(&mut world, |world| compile_once(world, &mut config))?;
```

当用户没传 `--timings` 时，`args.timings` 是 `None`，`new_or_placeholder` 返回一个 placeholder，随后的 `record` 会**原样跑编译但不导出**。这样 CLI 代码里就不必到处写 `if let Some(...) { ... } else { ... }`，逻辑被收敛进了 `Timer` 内部。

#### 4.1.2 核心流程

三种构造的字段取值与副作用对比如下：

| 构造方式 | `path` | `iter` | 是否调用 `enable()` | 后续 `record` 行为 |
| --- | --- | --- | --- | --- |
| `new(path)` | `Some(path)` | `0` | **是** | 真正清场→跑 `f`→导出 JSON |
| `placeholder()` | `None` | `0` | **否** | 直接跑 `f`，不导出 |
| `new_or_placeholder(Some)` | `Some` | `0` | 是 | 同 `new` |
| `new_or_placeholder(None)` | `None` | `0` | 否 | 同 `placeholder` |

有一个极易被忽略、但非常关键的细节：**`enable()` 发生在构造时（`new`），而不是 `record` 里**。这意味着从 `Timer::new(...)` 那一刻起，整个进程的 `timed!` 埋点就已经全部激活了。如果你在 `new` 之后、`record` 之前穿插跑了别的代码，那些代码里的 `timed!` 也会往 `EVENTS` 里塞事件。这直接解释了为什么 `record` 开头必须 `clear()`——见 4.2。

#### 4.1.3 源码精读

先看 `new`（[crates/typst-kit/src/timer.rs#L33-L37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L33-L37)）——注意它第一行就是 `enable()`：

```rust
pub fn new(path: PathBuf) -> Self {
    // Enable event collection.
    typst_timing::enable();
    Self { path: Some(path), iter: 0 }
}
```

而 `enable()` 本体只是把那个全局原子布尔翻成 `true`（[crates/typst-timing/src/lib.rs#L67-L72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L67-L72)）：

```rust
#[inline]
pub fn enable() {
    ENABLED.store(true, Ordering::Relaxed);
}
```

再看 `placeholder`（[crates/typst-kit/src/timer.rs#L42-L44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L42-L44)）——**没有任何副作用**，只是把 `path` 设成 `None`：

```rust
pub fn placeholder() -> Self {
    Self { path: None, iter: 0 }
}
```

`new_or_placeholder` 则是两者的分发器（[crates/typst-kit/src/timer.rs#L48-L53](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L48-L53)）：

```rust
pub fn new_or_placeholder(path: Option<PathBuf>) -> Self {
    match path {
        Some(path) => Self::new(path),
        None => Self::placeholder(),
    }
}
```

真实调用方在 typst-cli：`compile` 与 `watch` 都用 `new_or_placeholder`（[crates/typst-cli/src/compile.rs#L39](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L39)、[crates/typst-cli/src/watch.rs#L19](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/watch.rs#L19)）。命令行参数就是 `--timings`，它把路径作为 `Option<PathBuf>` 传进来（[crates/typst-cli/src/args.rs#L387-L393](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/args.rs#L387-L393)），其帮助文本里也写明产物可加载进 [Perfetto](https://ui.perfetto.dev)。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是验证你对三种构造副作用的判断。

1. **实践目标**：在不运行的情况下，预测下面两段「伪调用」之后 `is_enabled()` 与 `record` 的行为差异。
2. **操作步骤**：
   - 对照 [timer.rs#L33-L53](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L33-L53) 与 [lib.rs#L67-L86](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L67-L86)。
   - 情形 A：`let t = Timer::new("t.json".into());` 之后立即读 `typst_timing::is_enabled()`。
   - 情形 B：`let t = Timer::placeholder();` 之后立即读 `typst_timing::is_enabled()`。
3. **需要观察的现象**：情形 A 应返回 `true`，情形 B 应返回 `false`。
4. **预期结果**：情形 B 下，即便随后调用 `t.record(...)`，其内部 `f` 里的所有 `timed!` 都会被零成本门控挡掉（u3-l1），最终也不会写任何文件。
5. **待本地验证**：如果你本地搭好了 typst-kit 的最小依赖，可以写两行 `assert!` 验证；否则以上推理即为结论。

#### 4.1.5 小练习与答案

**练习 1**：假设有人误把 `Timer::new` 里的 `typst_timing::enable();` 删掉了，但 `record` 里其余逻辑不变。运行 `typst compile --timings out.json hello.typ` 会观察到什么？

> **答案**：`enable()` 不被调用，`ENABLED` 保持 `false`，于是 `f`（编译过程）里所有 `timed!` 都返回 `None`、不往 `EVENTS` push 任何事件。`record` 末尾的 `export_json` 仍然会创建 `out.json` 并写出一个合法的 JSON 数组——但数组是**空的** `[]`。打开 Perfetto 看到的是一张空时间轴。

**练习 2**：为什么 `placeholder()` 故意不调用 `enable()`，而不是「调用了但导出时跳过」？

> **答案**：不 `enable()` 才能让 `timed!` 走零成本门控（`with_span` 直接返回 `None`），既不加锁也不分配、更不取时间戳。如果「enable 了但导出时跳过」，那编译期间仍会白白产生成千上万条事件并写进全局 `EVENTS`，开销完全浪费。placeholder 的全部意义就是「让代码路径统一，同时保持关闭态零成本」。

---

### 4.2 record 的端到端时序：enable → clear → f → export_json 与 {n} 编号

#### 4.2.1 概念说明

`record` 是 `Timer` 唯一的「动作」方法。它把传入的闭包 `f` 包裹在**一次完整的计时会话**里。可以这样理解它的职责：

> 给我一段要计时的函数 `f`，我保证：跑之前把账本清干净；跑的时候让遍布代码库的 `timed!` 自动记账；跑完把账本导成 Chrome Trace JSON 写进 `path`，并把你 `f` 的返回值原样还给你。

注意三个「故意为之」的设计：

1. **`enable` 不在 `record` 里**：它在 `new` 里就做过了。`record` 假定开关已开。
2. **`clear` 在 `f` 之前**：因为 `EVENTS` 是全局共享、跨调用累积的（u2-l2）。从 `new()` 到第一次 `record` 之间，或上一次 `record` 之后，都可能已经有事件混进去。开头 `clear()` 保证导出的只是「本次 `f`」的事件。
3. **导出在 `f` 之后**：必须等 `f` 跑完，所有 `timed!` 的 `Start`/`End`（包括 RAII 的 `Drop`）都落账了，才能导出。

还有一个 watch 模式专属的问题：`typst watch` 会在一个循环里**反复**调用同一个 `Timer` 的 `record`（[crates/typst-cli/src/watch.rs#L60](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/watch.rs#L60)、[watch.rs#L79](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/watch.rs#L79)），每次重编译都要导出一个文件。如果路径固定，后一次会覆盖前一次。为此 `Timer` 引入了 `{n}` 占位符与 `iter` 计数器：路径里写 `{n}`，每次 `record` 把它替换成递增的序号，生成一组不互相覆盖的文件。

#### 4.2.2 核心流程

一次 `record` 的完整时间线如下（带 `f` 内部并行发生的埋点）：

```text
[构造阶段，早已发生]
  Timer::new(path) ──► typst_timing::enable()   # 全局开关已打开
                      │ ENABLED = true
                      └ 所有 timed! 从此刻起激活

[record 调用开始]
  ① self.path 是 None？ ──► 是：直接 return Ok(f(world))，不导出（placeholder 分支）
  ② typst_timing::clear()        # 清空 EVENTS 里的历史残留
  ③ 解析路径里的 {n}：
       - 若含 {n}：用 self.iter 替换它（如 "out-{n}.json" + iter=2 → "out-2.json"）
       - 若不含 {n} 且 iter>0：bail! 报错（多次导出必须带 {n}）
  ④ output = f(world)            # ★ 执行被计时的函数
       │  期间：遍布编译器的 timed! / #[time] 不断 push Start/End 进 EVENTS
       └  f 返回，所有 TimingScope 已 Drop，End 事件已落账
  ⑤ self.iter += 1               # 序号递增（为下一次 record 做准备）
  ⑥ File::create(path) + BufWriter(1 MiB)
  ⑦ typst_timing::export_json(writer, |span| resolve_span(world, span))
       │  把 EVENTS 序列化成 Chrome Trace JSON 写入文件
       └  source 闭包把每个 span 还原成 (file, line)
  ⑧ Ok(output)                   # 把 f 的返回值原样回传
```

两个值得记牢的顺序：`clear` 在 `f` **之前**；`iter += 1` 在 `f` **之后**、导出**之前**。后者意味着第一次 `record`（`iter` 从 0 开始）写出的文件是 `...0.json`，第二次是 `...1.json`，以此类推。

还有一个 Rust 借用检查上的精妙安排：`f` 先拿走 `world` 的**可变**借用去编译；`f` 返回后，`export_json` 的闭包再以**共享**借用读取 `world` 来解析 span（`resolve_span` 只需要 `&W`）。两者按先后顺序发生，互不冲突。我们把 `world` 同时用作「编译输入」和「span 解析字典」。

#### 4.2.3 源码精读

`record` 的完整签名（[crates/typst-kit/src/timer.rs#L57-L61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L57-L61)）：

```rust
pub fn record<W: World, T>(
    &mut self,
    world: &mut W,
    f: impl FnOnce(&mut W) -> T,
) -> StrResult<T> {
```

- 泛型 `W: World`：只要有 `World` 实现即可，既能在真实 CLI 的 `SystemWorld` 上跑，也能在测试用的 mock world 上跑。
- 泛型 `T`：`f` 返回什么，`record` 就回传什么。返回值用 `StrResult<T>` 包裹，导出失败时变成字符串错误。

先是 placeholder 短路（[timer.rs#L62-L64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L62-L64)）——注意这里**只跑 `f`，不 `clear`、不导出**：

```rust
let Some(path) = &self.path else {
    return Ok(f(world));
};
```

接着是清场与 `{n}` 校验（[timer.rs#L66-L72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L66-L72)）：

```rust
typst_timing::clear();

let string = path.to_str().unwrap_or_default();
let numbered = string.contains("{n}");
if !numbered && self.iter > 0 {
    bail!("cannot export multiple recordings without `{{n}}` in path");
}
```

`clear()` 本体就是给 `EVENTS` 加锁后清空（[crates/typst-timing/src/lib.rs#L88-L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L88-L92)）。注意那句 `bail!`：路径不带 `{n}` 时只允许导出一次（`iter` 必须为 0），否则第二次 `record` 会直接报错——这是为了防止 watch 模式下静默覆盖之前的记录。

`{n}` 的实际替换（[timer.rs#L74-L80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L74-L80)）：

```rust
let storage;
let path = if numbered {
    storage = string.replace("{n}", &self.iter.to_string());
    Path::new(&storage)
} else {
    path.as_path()
};
```

这里用了一个常见的小技巧：`storage` 在 `if` 分支里绑定，但其生命周期被外层 `let storage;` 提前声明延长，从而让 `Path::new(&storage)` 能在两个分支里给出统一类型的 `path` 借用。

接下来是「跑函数 + 递增序号」（[timer.rs#L82-L83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L82-L83)）：

```rust
let output = f(world);
self.iter += 1;
```

最后是建文件、起缓冲、导出（[timer.rs#L85-L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L85-L92)）：

```rust
let file =
    File::create(path).map_err(|e| format!("failed to create file: {e}"))?;
let writer = BufWriter::with_capacity(1 << 20, file);

typst_timing::export_json(writer, |span| {
    resolve_span(world, Span::from_raw(span))
        .unwrap_or_else(|| ("unknown".to_string(), 0))
})?;

Ok(output)
```

几个要点：

- `BufWriter::with_capacity(1 << 20, file)`：配 1 MiB 缓冲，避免每写一个事件就触发一次系统调用。Chrome Trace JSON 通常很大（编译一次能产生上万条事件），缓冲很必要。
- 闭包 `|span| resolve_span(world, Span::from_raw(span)).unwrap_or_else(...)`：这正是 u2-l4 里那个 `source` 闭包的**真实实现**。`span` 是 `NonZeroU64`，先用 `Span::from_raw` 还原成 `typst_syntax::Span`，再交给 `resolve_span` 翻译；解析失败时回退成 `("unknown", 0)`，保证导出永远不会因单个坏 span 而中断。
- 这里的 `export_json` 内部还会用 `std::mem::take` 把 `EVENTS` 取空（u2-l4 讲过的防死锁技巧），所以本次导出后缓冲区自然是空的——但不影响下次 `record` 开头仍要 `clear()`，因为「`enable` 之后、下次 `record` 之前」仍可能有事件混入。

#### 4.2.4 代码实践

这是一个**可真实运行**的实践，用 typst CLI 触发完整的 `record` 链路。

1. **实践目标**：亲手产出一份 Chrome Trace JSON，并在 Perfetto 里看到 typst 编译各阶段的时间轴。
2. **操作步骤**：
   - 准备一个 `hello.typ`，内容随意，例如 `#set page(width: 10cm); Hello {1+1}.`
   - 运行（需 typst-cli 启用了 `timer` feature）：
     ```bash
     typst compile --timings trace.json hello.typ
     ```
   - 打开 [https://ui.perfetto.dev](https://ui.perfetto.dev)，把生成的 `trace.json` 拖进去。
3. **需要观察的现象**：Perfetto 里应出现一排排彩色色块（B/E 事件对），名为 `compile once`、各类语法/求值/排版阶段的耗时一览无余；每条事件在 `args` 里还带有 `file`/`line`（如果该事件带 span）。
4. **预期结果**：`trace.json` 是一个合法的 JSON 数组，首元素 `ph` 为 `"B"`；时间轴上能区分出不同线程（`tid`）。
5. **进阶（watch + `{n}`）**：把命令换成 `typst watch --timings iter-{n}.json hello.typ`，保存几次源文件后退出，观察目录下生成了 `iter-0.json`、`iter-1.json`…… 这正对应 `record` 里 `self.iter` 从 0 递增。**待本地验证**（watch 行为依赖你本地的文件监听是否可用）。

#### 4.2.5 小练习与答案

**练习 1**：把 4.2.2 时间线里的「`self.iter += 1`」挪到 `f(world)` **之前**，会对导出结果产生什么影响？

> **答案**：导出的 JSON 内容**不变**（事件来自 `f` 的执行，与 `iter` 无关），但 `{n}` 替换会用「递增后」的序号。原本第一次 `record` 写 `out-0.json`，挪动后会写 `out-1.json`，且 `out-0.json` 永远不会出现。序号会整体右移一位。注意「`!numbered && self.iter > 0`」的校验若也跟着前移，逻辑会更绕——这正是源码把 `iter += 1` 放在 `f` 之后的理由：保持「第 0 次 record 写序号 0」的直觉。

**练习 2**：假设 `record` 里删掉开头的 `typst_timing::clear();`，在 `typst watch` 反复编译时会观察到什么？

> **答案**：`export_json` 内部的 `std::mem::take` 会在每次导出后清空 `EVENTS`（u2-l4），所以「单次 record 内」不会有残留。但是，两次 `record` 之间的间隙里（开关仍为 `enable` 状态）若任何代码触发了 `timed!`，这些事件会留存到下一次导出，混进下一次的 JSON。开头 `clear()` 是把这道「跨 record 的缝隙」也堵上的双保险。

---

### 4.3 resolve_span：借助 World 把裸 span 还原成 (file, line)

#### 4.3.1 概念说明

回忆 u3-l2 与 u2-l4：`Event.span` 是 `Option<NonZeroU64>`，typst-timing **故意**不认识 `typst_syntax::Span`，否则 typst-syntax 就反向依赖了 typst-timing，形成循环依赖。所以一个 span 被记录时，它「退化」成了一个裸数字；这个数字本身对人类毫无意义——你不知道它指向哪个文件、哪一行。

「把裸数字还原成 `(file, line)`」就是一笔**欠账**：typst-timing 欠下的，得到能同时依赖 typst-timing 和 typst-syntax 的「上层」去还。typst-kit 正好是这个上层，`resolve_span` 就是还款动作。它的桥梁是 `World` trait：因为 `World` 知道「某个文件 id 对应的源码内容是什么」，用它就能把 span 定位到具体的字节偏移，再换算成行号。

`resolve_span` 要处理三种 span 形态（对应 `SpanKind` 的三个变体）：

- **Detached（游离）**：不指向任何文件，返回 `None`。
- **Number（编号 span）**：Typst 源码文件最常见的形式——每个语法树节点有个唯一编号。需要先取 `Source`（解析后的语法树），用编号查到字节区间，再换行。
- **Range（区间 span）**：直接是一段字节区间，通常来自非 Typst 源码的文本。需要取原始 `Bytes`，按行换算。

#### 4.3.2 核心流程

`resolve_span(world, span)` 的还原链路：

```text
输入：span: NonZeroU64（来自 Event.span）
  │
  ▼ Span::from_raw(span)        # 裸数字 → typst_syntax::Span（一个 8 字节包装）
  │
  ▼ span.get()                  # 解包成 SpanKind 枚举
  │
  ├── SpanKind::Detached ─────────────► 返回 None
  │
  ├── SpanKind::Number { id, num }
  │     ▼ world.source(id)            # 通过 World 取该文件的 Source（语法树）
  │     ▼ source.range(num, None)     # 把 span 编号映射成字节区间 Range<usize>
  │     ▼ source.lines().byte_to_line(range.start)  # 字节偏移 → 行号
  │     └── (id, line)
  │
  └── SpanKind::Range { id, range }
        ▼ world.file(id)              # 通过World 取该文件的原始 Bytes
        ▼ file.lines()                # 按 UTF-8 切行（非 UTF-8 则失败 → None）
        ▼ lines.byte_to_line(range.start)
        └── (id, line)
  │
  ▼ Some((format!("{id:?}"), line as u32 + 1))   # 行号转 1-based，文件名用 FileId 的 Debug
```

最终返回的元组里，行号做了 `+ 1`（从 0-based 转成程序员更习惯的 1-based），文件名则是 `format!("{id:?}")`——即 `FileId` 的 `Debug` 输出，而非真实磁盘路径。这是一个稳健但简化的选择：`FileId` 内部并不一定绑定完整路径，用它的 Debug 既能区分文件又不会出错。回到 `record` 里，调用方还套了 `unwrap_or_else(|| ("unknown".to_string(), 0))`，所以即便 `resolve_span` 返回 `None`（如 Detached span），导出也只会标注 `("unknown", 0)` 而不会失败。

#### 4.3.3 源码精读

先看入口：闭包里那句 `Span::from_raw(span)` 把裸数字还原成 `Span`（[timer.rs#L89-L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L89-L92)）。`Span` 本质就是一个 `NonZeroU64` 的新类型（[crates/typst-syntax/src/span.rs#L62-L63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L62-L63)），`from_raw` 几乎是个恒等映射（[span.rs#L135-L142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L135-L142)）：

```rust
pub struct Span(NonZeroU64);
// ...
pub const fn from_raw(v: NonZeroU64) -> Self {
    Self(v)
}
```

`Span::get` 才是真正的「解包」，它把 8 字节里的高 16 位（文件 id）和低 48 位（编号或区间）拆开，归入三个变体之一（[span.rs#L177-L187](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L177-L187)）。`SpanKind` 的定义见 [span.rs#L75-L83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L75-L83)：

```rust
pub enum SpanKind {
    Detached,
    Number { id: FileId, num: SpanNumber },
    Range { id: FileId, range: Range<usize> },
}
```

现在看 `resolve_span` 本体（[timer.rs#L99-L116](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L99-L116)）：

```rust
fn resolve_span<W: World>(world: &W, span: Span) -> Option<(String, u32)> {
    let (id, line) = match span.get() {
        SpanKind::Detached => return None,
        SpanKind::Number { id, num } => {
            let source = world.source(id).ok()?;
            let range = source.range(num, None)?;
            let line = source.lines().byte_to_line(range.start)?;
            (id, line)
        }
        SpanKind::Range { id, range } => {
            let file = world.file(id).ok()?;
            let lines = file.lines().ok()?;
            let line = lines.byte_to_line(range.start)?;
            (id, line)
        }
    };
    Some((format!("{id:?}"), line as u32 + 1))
}
```

逐行对照：

- `world.source(id)` 与 `world.file(id)` 是 `World` trait 的两个加载方法（[crates/typst-library/src/lib.rs#L73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L73)、[lib.rs#L80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L80)）：前者返回解析好的 `Source`，后者返回原始 `Bytes`。`.ok()?` 把 `FileResult`（失败时）转成 `None`，跳过这条 span。
- `source.range(num, None)`：用 span 编号在语法树里查出对应的字节区间（[crates/typst-syntax/src/source.rs#L129-L142](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L129-L142)），第二个参数 `None` 表示不要子区间。
- `source.lines()`：拿到该文件的行表 `Lines<String>`（[source.rs#L74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L74)）。
- `byte_to_line(range.start)`：在行表里二分查找，把字节偏移换算成行号（[crates/typst-syntax/src/lines.rs#L67-L74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L67-L74)）。
- Range 分支多了一层 `file.lines().ok()?`：`Bytes::lines()` 返回 `Result<Lines<String>, Utf8Error>`——若文件不是合法 UTF-8（比如二进制文件），切行失败，用 `.ok()?` 跳过（[crates/typst-library/src/foundations/bytes.rs#L158](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/bytes.rs#L158)）。

注意整个函数大量使用 `?`：任何一步失败（文件加载不到、span 编号查不到、字节越界、非 UTF-8）都会优雅地返回 `None`，再由 `record` 里的 `unwrap_or_else` 兜成 `("unknown", 0)`。这是一种「单条 span 解析失败不影响整体导出」的稳健策略。

#### 4.3.4 代码实践

这是一个**源码阅读 + 调用链跟踪**型实践。

1. **实践目标**：把 span 从「裸数字」到「`(file, line)`」经过的每一跳都对应到具体源码，画出调用链。
2. **操作步骤**：
   - 从 [timer.rs#L89-L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L89-L92) 出发，记下 `span`（`NonZeroU64`）。
   - 跳到 `Span::from_raw`（[span.rs#L140](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L140)）→ `Span::get`（[span.rs#L177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L177)），判断它落入哪个 `SpanKind`。
   - 对 `Number` 变体，依次打开 `World::source`（[lib.rs#L73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L73)）→ `Source::range`（[source.rs#L129](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L129)）→ `Source::lines`（[source.rs#L74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L74)）→ `Lines::byte_to_line`（[lines.rs#L67](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L67)）。
3. **需要观察的现象**：每一跳的输入输出类型变化：`NonZeroU64 → Span → SpanKind → (FileId, usize 字节偏移) → (FileId, usize 行号) → (String, u32)`。
4. **预期结果**：你能画出一张 6 步的调用链图，并指出「从 `NonZeroU64` 到字节偏移」这一跳发生在 `Source::range` 内部（它用语法树节点的 `find_number` 查找），「从字节偏移到行号」这一跳发生在 `byte_to_line`（它做二分查找）。
5. **思考题（待本地验证）**：如果一个 span 指向的源文件在导出时已被 `World` 移出缓存、`world.source(id)` 返回 `Err`，这条事件在 JSON 里会显示成什么？答案是 `args` 为 `{file: "unknown", line: 0}`——因为 `resolve_span` 返回 `None`，被 `unwrap_or_else` 兜底。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `resolve_span` 里对 `SpanKind::Range` 分支用的是 `world.file(id)`（取原始字节）而不是 `world.source(id)`（取语法树）？

> **答案**：`Range` span 本来就「自带字节区间」（`range: Range<usize>`），不需要再去语法树里查编号，只需要一个能把字节偏移换算成行号的行表。而行表可以直接从原始字节 `Bytes::lines()` 得到。`Source` 对应的是「已解析为 Typst 语法树」的文件，而 `Range` span 往往来自非 Typst 源码的文本（如解析 JSON 出错时的位置），不一定有对应的 `Source`，用 `file` 更通用。

**练习 2**：最终返回的文件名是 `format!("{id:?}")`（`FileId` 的 Debug），而不是真实磁盘路径。这样做有什么取舍？

> **答案**：好处是稳健——`FileId` 是个轻量句柄，不一定始终绑定到完整路径（可能是包内虚拟文件、内存文件等），强行求路径可能失败或开销大；用 Debug 必然能得到一个可区分的字符串。代价是对人类不够友好（看到的是一串 id 而非 `hello.typ`）。如果想换成真实路径，需要额外让 `World` 暴露「id → 路径」的能力，但这超出了计时导出的最小职责。当前实现选择了「够用且不出错」。

---

## 5. 综合实践

把三个模块串起来，做一次端到端的「跟踪一次真实 `record`」。

**任务**：对照 [crates/typst-kit/src/timer.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs) 与 [crates/typst-cli/src/compile.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs)，为下面这条命令写一份完整的「执行说明书」：

```bash
typst compile --timings trace.json hello.typ
```

要求你按时间顺序写出以下每一项**发生在哪段源码、调用了什么**：

1. **构造**：`Timer::new_or_placeholder(Some("trace.json"))` 走 `new` 分支，立即 `enable()`——开关打开（对应 [timer.rs#L33-L37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L33-L37)）。
2. **清场**：`record` 开头 `typst_timing::clear()` 清空历史事件（[timer.rs#L66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L66)）。
3. **跑函数**：`f(world)` 执行 `compile_once`（[compile.rs#L47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L47)、[compile.rs#L257-L261](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-cli/src/compile.rs#L257-L261)）；期间 `#[time(name = "compile once")]` 等宏不断把 `Start`/`End` push 进 `EVENTS`。
4. **递增**：`self.iter += 1`（为下一次做准备）。
5. **建文件 + 缓冲**：`File::create` + `BufWriter(1 MiB)`（[timer.rs#L85-L87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L85-L87)）。
6. **导出**：`export_json` 把事件序列化写入；闭包对每个带 span 的事件调用 `Span::from_raw` → `resolve_span`，经 `World::source`/`file` 还原出 `(file, line)`（[timer.rs#L89-L92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L89-L92)）。

**额外动手环节**：仿照本讲的「调用方统一」思想，自己写一段骨架代码（伪代码即可），让同一段 `f` 既能被「真计时」路径运行、也能被 placeholder 路径运行，并验证 placeholder 路径下 `trace.json` **不会被创建**：

```rust
// 示例代码（仅示意 Timer 的用法，不是项目原有代码）
fn run(world: &mut World, timings: Option<PathBuf>) {
    let mut timer = Timer::new_or_placeholder(timings);
    // 无论 timings 是否为 Some，下面这一行都长得一样：
    timer.record(world, |world| {
        // ... 这里放真正要计时的逻辑 ...
    }).ok();
}
```

**验证要点**：

- 传 `Some("out.json")`：`new` 分支 `enable`，`record` 创建 `out.json` 并写出 JSON 数组。
- 传 `None`：`placeholder` 分支不 `enable`，`record` 在 `let Some(path) = &self.path else { return Ok(f(world)); }` 处直接返回，**不创建任何文件**，且 `f` 内部的 `timed!` 全部因 `is_enabled() == false` 而零成本跳过。

**待本地验证**：完整运行需要一个实现了 `World` 的类型（typst-cli 里是 `SystemWorld`），搭环境较重；如果你只想验证逻辑，阅读 [timer.rs#L57-L95](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-kit/src/timer.rs#L57-L95) 并口述两条路径即可。

---

## 6. 本讲小结

- `Timer` 是 typst-timing 唯一的真实用户，把 `enable / clear / export_json` 三件散装原语固化为「记录一次函数执行并写盘」的对象，只有 `path: Option<PathBuf>` 与 `iter: usize` 两个字段。
- 三种构造里，只有 `new(path)` 会调用 `enable()`；`placeholder()` 完全无副作用，目的是让调用方代码路径统一（都写 `timer.record(...)`），同时保持关闭态零成本。
- `record` 的时间线是：`clear` → 解析 `{n}` → `f(world)` → `iter += 1` → `File::create` + `BufWriter` → `export_json`；`enable` 不在 `record` 内，而在 `new` 里，所以开头必须 `clear()` 清掉缝隙期混入的事件。
- `{n}` 占位符配合 `iter` 计数器，让 `typst watch` 反复编译时能生成 `0.json`、`1.json` 一组互不覆盖的文件；不带 `{n}` 时只允许导出一次。
- `resolve_span` 是 typst-timing「不依赖 typst-syntax」欠账的还款点：用 `Span::from_raw` 把裸 `NonZeroU64` 还原成 `Span`，再借 `World` 的 `source`/`file` 取文件内容、把字节偏移换算成行号，最终输出 `(file, line)`，失败时被兜底为 `("unknown", 0)`。
- `resolve_span` 对 `Number` span 走语法树（`Source::range`），对 `Range` span 走原始字节（`Bytes::lines`），两种路径都大量用 `?` 优雅降级，保证单条坏 span 不影响整体导出。

## 7. 下一步学习建议

到这里，typst-timing 的全部内容（API、全局状态、时间戳、导出、宏、零成本门控、wasm、集成）已经讲完。接下来你可以：

1. **跟一次真实编译**：按本讲 4.2.4 的实践跑 `typst compile --timings trace.json`，在 Perfetto 里对照时间轴去源码里搜 `#[time(...)]` 属性宏（遍布 syntax、eval、layout、render 等 crate），看每个色块对应哪段代码。这是把「读源码」和「看现象」结合的最佳练习。
2. **读 typst-macros 的 `#[time]` 属性宏**（[crates/typst-macros/src/time.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs)）：本手册 u3-l2 已讲过它的展开逻辑，现在你可以带着「它生成的 `TimingScope` 最终如何被 `Timer` 收集」的视角重读一遍，闭环整个埋点链路。
3. **研究 `World` trait 的更多方法**（[crates/typst-library/src/lib.rs#L60-L98](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L60-L98)）：本讲只用到了 `source` 和 `file`。`World` 是 Typst 与宿主环境之间最重要的边界接口，理解了它，你就能写出一个自己的 `World` 实现（例如内存里的 mock world），从而在脱离 typst-cli 的情况下也能驱动 `Timer` 做计时。
4. **思考一个扩展题**：如果要让导出的 JSON 里 `args.file` 显示真实磁盘路径而非 `FileId` 的 Debug，你会怎么改？提示——需要让 `World`（或 `resolve_span` 的调用方）额外提供「id → 路径」的能力，并权衡路径可能不存在的稳健性。这能帮你从「读懂」走向「能改」。
