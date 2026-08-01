# 快速上手：启用计时与 timed! 宏

## 1. 本讲目标

u1-l1 让我们俯瞰了 typst-timing 是什么、长什么样。本讲是**第一次动手**：我们要把这套计时设施真正「跑起来」，亲手记录第一对事件。

读完本讲，你应该能够：

- 正确使用 `enable()` / `disable()` / `is_enabled()` 这三个全局开关函数，并说清它们读写的是同一个全局原子布尔；
- 用 `timed!` 宏的**两种形式**包裹一段代码：带 `span = ...` 和不带 `span`，并能口述它们分别展开成什么；
- 理解 `TimingScope` 的 **RAII 模型**——创建时记一条 `Start` 事件，离开作用域被 `Drop` 时记一条 `End` 事件，两者天然配成一对；
- 看懂 Typst 源码里真实的计时埋点（如 `typst-syntax` 的 `parse`、`typst` 的循环迭代检查），知道「别人是怎么用的」。

本讲**只关心「怎么用、为什么会这样」**，刻意不深入「禁用态为什么零成本」「wasm 怎么取时间」等问题——它们分别留给 u3-l1 与 u2/u3 详讲。

## 2. 前置知识

请确认你对下面几个概念有基本印象，不熟也没关系，我们会顺带复习。

- **承接 u1-l1**：typst-timing 是 Typst 的基础设施型 crate，提供一个全局开关（默认关闭）、一套记录事件的写法（`timed!` / `TimingScope`）、一个导出函数（`export_json`）。本讲全部建立在这个认识之上。
- **RAII（资源获取即初始化）**：Rust 管理资源的核心思想。一个对象的「创建」和「销毁」由它的作用域决定：进入作用域时构造、离开作用域时自动调用 `Drop::drop`。typst-timing 正是利用 `Drop` 来自动记录「结束」事件——你不需要手写「结束计时」的代码。
- **`Drop` trait**：给一个类型实现 `Drop` 后，该类型的每个实例离开作用域时，Rust 会自动调用它的 `drop(&mut self)` 方法。typst-timing 的 `TimingScope` 就实现了 `Drop`。
- **`Option<T>` 与 `Drop` 的配合**：当一个 `Option<T>` 被销毁时，只有当它是 `Some(t)` 时才会触发内部 `t` 的 `drop`；`None` 什么都不做。本讲你会看到这个性质如何天然实现「关闭时什么都不记录」。
- **`macro_rules!` 宏的直觉**：Rust 的声明宏通过「模式匹配 + 文本替换」生成代码。你不需要会写宏，只要能看懂 `timed!` 展开后变成了什么普通 Rust 代码即可。
- **`&'static str`**：字符串字面量（如 `"parse"`）的类型，它在编译期就确定、整个程序运行期间都有效，因此传递它「零分配、零克隆」。`timed!` 的名字就是这种类型。

> 术语提示：本讲反复出现的「`Start`/`End` 事件」「作用域」「span」会在后文逐一展开。你只要先记住一句话：**一段被计时的代码会产生「开始 + 结束」两条记录，配成一对**。

## 3. 本讲源码地图

本讲以 typst-timing 的单文件实现为主，辅以两个「真实使用点」作印证。

| 文件 | 作用 |
| --- | --- |
| [`crates/typst-timing/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs) | 本讲的绝对主角。`timed!` 宏、全局开关 `enable/disable/is_enabled`、`TimingScope` 的创建与 `Drop` 全在这一个文件里。 |
| [`crates/typst-syntax/src/parser.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs) | 真实使用样例：解析器的三个入口函数都用 `TimingScope::new(...)` 包裹了整个解析过程。 |
| [`crates/typst/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | 真实使用样例：排版主循环里用 `timed!(...)` 宏包裹了一次「稳定性检查」。 |

> 说明：后两个文件不在本讲 permalink base（`crates/typst-timing/`）覆盖范围内，但我们仍给出指向当前 HEAD 的完整 GitHub 链接，方便你直接点开对照。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，按「先开开关 → 再用宏 → 最后理解它为什么自动配对」的顺序展开。

### 4.1 全局开关：enable / disable / is_enabled

#### 4.1.1 概念说明

typst-timing 默认是**关闭**的（u1-l1 已提到，开关初值为 `false`）。这意味着：如果你不显式打开它，所有计时埋点都几乎不产生开销（具体原因 u3-l1 会剖析）。

要把计时打开，使用者需要调用一个全局函数 `enable()`。与之配套的还有两个：

- `enable()`：把开关置为「开」。
- `disable()`：把开关置为「关」。
- `is_enabled()`：查询当前是开还是关。

这三个函数操作的是**同一个全局开关**——一个进程级别的原子布尔变量 `ENABLED`。所以你在任意线程调用 `enable()`，整个进程的所有线程都会看到「计时已开启」。这是一个「一次性、全局生效」的开关，而不是「每次调用计时 API 时单独传参」。

#### 4.1.2 核心流程

开关的三态切换可以这样理解：

```
进程启动：ENABLED = false（默认关闭）
   │
   ├── 调用 enable()  ──► ENABLED.store(true)  ──► 此后 is_enabled() == true
   │
   ├── 调用 disable() ──► ENABLED.store(false) ──► 此后 is_enabled() == false
   │
   └── 调用 is_enabled() ──► 读取 ENABLED 当前值（true / false）
```

要点：

- 三个函数都只读写**一个**静态变量 `ENABLED`，彼此完全联动。
- 它们不接收任何参数，也不返回复杂结果（`enable`/`disable` 返回单元 `()`，`is_enabled` 返回 `bool`）。
- 后续所有计时埋点（`timed!`、`TimingScope::new`）在「决定要不要真的记录」时，内部都是去读这个 `ENABLED`。

#### 4.1.3 源码精读

三个函数的实现都极短，且都用 `Ordering::Relaxed`（本讲只需记住「这只保证原子性、最轻量」，内存序的取舍留给 u2-l2）：

> [src/lib.rs:66-72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L66-L72)：`enable()` 把全局 `AtomicBool` 写为 `true`。注释说明「只需要原子性、不需要同步其它操作，所以 `Relaxed` 足够」。
>
> [src/lib.rs:74-80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L74-L80)：`disable()` 把同一个变量写回 `false`。
>
> [src/lib.rs:82-86](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L82-L86)：`is_enabled()` 读取当前值并返回 `bool`。

它们读写的目标，就是 u1-l1 见过的那个静态量：

> [src/lib.rs:60-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L60-L61)：`static ENABLED: AtomicBool = AtomicBool::new(false);`——初值 `false` 正是「默认关闭」的来源。

#### 4.1.4 代码实践

这是一个**最小调用型实践**，目标是亲手验证开关的联动。

1. **实践目标**：确认 `enable` / `disable` / `is_enabled` 三者操作的是同一个状态，且默认为 `false`。
2. **操作步骤**：在一个已引入 typst-timing 的二进制项目（u1-l1 综合实践已建过）里，把 `src/main.rs` 写成（**示例代码**，由本讲提供）：
   ```rust
   use typst_timing::{disable, enable, is_enabled};

   fn main() {
       println!("初始: {}", is_enabled()); // 期望 false
       enable();
       println!("enable 后: {}", is_enabled()); // 期望 true
       disable();
       println!("disable 后: {}", is_enabled()); // 期望 false
   }
   ```
   然后 `cargo run`。
3. **需要观察的现象**：连续三行布尔输出。
4. **预期结果**：
   ```
   初始: false
   enable 后: true
   disable 后: false
   ```
5. 本步骤假定你已按 u1-l1 的方式引入了 typst-timing；若版本拉取失败，改用 `path = "../crates/typst-timing"` 并标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果不调用 `enable()`，直接用 `timed!` 包裹代码，会发生什么？
**答案**：代码照常执行（`timed!` 会返回被包裹表达式的值），但因为开关是关的，内部不会真正记录任何事件——这是「默认关闭、几乎零开销」的设计。具体机制见 4.3 与 u3-l1。

**练习 2**：`is_enabled()` 读取的值，和 `enable()` 写入的值，是同一个变量吗？
**答案**：是。三者都作用于同一个 `static ENABLED: AtomicBool`（`src/lib.rs` 第 60-61 行），所以一处的修改全局可见。

---

### 4.2 timed! 宏：两种调用形式

#### 4.2.1 概念说明

`enable()` 只是打开了开关，真正「在某段代码上埋点」靠的是 `timed!` 宏。它接受三样东西：

1. 一个**名字** `name`（字符串字面量，如 `"parse"`），用来在时间轴上标注这一段；
2. 一个**可选的 span**（源码位置信息，写法是 `span = ...`）；
3. 一段**表达式** `$body`（你想计时的那段代码）。

`timed!` 会把这段表达式「包」进一个计时作用域里，并且**返回该表达式的值**——也就是说它不改变你的代码逻辑，只是顺手记录时间。它有两种调用形式：

```rust
// 形式一：带 span
timed!("my scope", span = 某个span值, 某段代码);

// 形式二：不带 span
timed!("my scope", 某段代码);
```

两种形式的区别**仅在于是否附带 span**：带了 span，导出的事件里就能附带「这段代码来自哪个文件、第几行」；不带 span，事件就只有名字、没有源码位置。后续的 `TimingScope` 与导出环节会用到这个 span（本讲 4.3 和综合实践会看到：不带 span 时，导出 JSON 的 `args` 字段会是 `null`）。

> 什么是 span？它本质是一个「源码位置的压缩编号」，在 typst-timing 里用一个裸数字 `NonZeroU64` 表示。之所以用裸数字而不是 `typst-syntax` 里的 `Span` 类型，是因为 `typst-timing` 不能反过来依赖 `typst-syntax`（否则会形成循环依赖）。真实 Typst 代码里，这个裸数字通常由 `Span::into_raw()` 得到（见 [crates/typst-syntax/src/span.rs:190](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L190)）。这套来龙去脉属于 **u3-l2** 的内容，本讲你只要知道「span 是可选的、是个裸数字」即可。

#### 4.2.2 核心流程

`timed!` 是用 `macro_rules!` 写的声明宏，它有**两个匹配分支**，分别对应「带 span」和「不带 span」。展开后，它们都只是「先创建一个计时作用域变量，再执行 body」：

```
形式一 timed!("foo", span = S, body)  展开为 ──┐
   {                                            │
     let __scope = TimingScope::with_span(      │  ← 先建作用域（此时记 Start）
         "foo", Some(S));                       │
     body                                       │  ← 再跑你的代码
   }                                            │  ← 离开块时 __scope 被销毁（此时记 End）

形式二 timed!("foo", body)            展开为 ──┘
   {
     let __scope = TimingScope::new("foo");     ← 先建作用域（记 Start）
     body                                       ← 再跑你的代码
   }                                            ← 销毁时记 End
```

两个分支几乎一样，唯一差别是调用 `with_span(..., Some(S))` 还是 `new(...)`（后者等价于 `with_span(..., None)`，见 4.3）。

注意一个关键顺序：**`let __scope = ...` 这一行先执行（记录 Start），然后才执行 `body`**；等整个块结束时，`__scope` 才被销毁（记录 End）。所以 Start 一定在 body 之前，End 一定在 body 之后——这正是我们想要的「包裹」语义。

#### 4.2.3 源码精读

宏定义本身就带了一段很清楚的文档注释和示例：

> [src/lib.rs:11-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L11-L44)：`timed!` 宏定义。注释里说明了「表达式的输出会被返回」「作用域命名为 `name`、span 可选」，并给了带/不带 span 两个示例。`#[macro_export]` 让它能在 crate 外以 `typst_timing::timed!` 或 `use typst_timing::timed;` 的方式使用。

两个匹配分支的骨架（关键部分）：

> [src/lib.rs:36-39](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L36-L39)：带 span 分支，展开为 `let __scope = $crate::TimingScope::with_span($name, Some($span)); $body`。
>
> [src/lib.rs:40-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L40-L43)：不带 span 分支，展开为 `let __scope = $crate::TimingScope::new($name); $body`。

> 几个细节：`$name:expr` 要求名字是一个表达式（实际几乎都是字符串字面量，类型为 `&'static str`）；`$(,)?` 允许末尾多一个可选的逗号；`$crate` 是宏里指代「定义本宏的 crate」的占位符，保证展开后在别的 crate 里也能正确找到 `TimingScope`。

**真实使用样例（带值返回）**——Typst 排版主循环里用 `timed!` 包裹一次布尔检查，并直接用它的返回值：

> [crates/typst/src/lib.rs:158](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L158-L158)：`if timed!("check stabilized", constraint.validate(...)) { ... }`——这里 `timed!` 包裹的是 `constraint.validate(...)` 这个返回布尔的调用，宏整体也返回该布尔值，于是可以直接放进 `if` 条件里。计时不改变逻辑，只是顺手记一笔。这里用的是「不带 span」形式。

#### 4.2.4 代码实践

这是一个**手工展开型实践**，帮你把「宏」还原成「普通代码」，破除对宏的陌生感。

1. **实践目标**：能口述 `timed!("foo", expr)` 展开后的等价普通 Rust 代码。
2. **操作步骤**：阅读 [src/lib.rs:36-43](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L36-L43) 两个分支；然后在不运行的前提下，把下面这段使用宏的代码「翻译」成不带宏的形式（写在纸上或注释里）：
   ```rust
   // 原始（使用宏）
   let n = timed!("compute", 1 + 2);
   ```
3. **需要观察的现象**：翻译结果应当是一个块表达式，块内先 `let __scope = ...`，再求值 `1 + 2`。
4. **预期结果**（参考答案）：
   ```rust
   // 翻译后（不带宏）
   let n = {
       let __scope = typst_timing::TimingScope::new("compute");
       1 + 2
   };
   ```
   如果开了 `enable()`，`__scope` 是 `Some(...)`，块结束时记一条 End；如果没开，`__scope` 是 `None`，结束时什么也不记。两种情况下 `n` 都等于 `3`。
5. 若你装了 `cargo-expand`，可用 `cargo expand` 查看真实展开结果对照；本步骤不要求运行。

#### 4.2.5 小练习与答案

**练习 1**：`timed!("foo", span = S, body)` 里的 `S` 最终被包进什么传给 `with_span`？
**答案**：被包进 `Some(S)`，即 `with_span("foo", Some(S))`。所以 `with_span` 的第二个参数类型是 `Option<NonZeroU64>`。

**练习 2**：`timed!` 宏会改变被包裹代码的返回值吗？
**答案**：不会。展开后 body 是块的尾表达式，块的值就是 body 的值；`__scope` 只是一个被丢弃的副作用载体（记录事件），不影响返回值。所以 `timed!("x", f())` 的返回值与 `f()` 完全相同。

**练习 3**：为什么宏里用 `$crate::TimingScope` 而不是直接 `TimingScope`？
**答案**：`$crate` 是 Rust 宏的卫生占位符，展开后会替换成「定义该宏的 crate 的路径」（即 `typst_timing`）。这样当别的 crate（如 typst-syntax、typst）调用 `timed!` 时，也能正确解析到 `typst_timing::TimingScope`，不会因为调用方的命名空间而找错类型。

---

### 4.3 TimingScope 的 RAII 模型：创建记 Start，Drop 记 End

#### 4.3.1 概念说明

`timed!` 展开后出现了 `TimingScope::new` / `with_span`。`TimingScope` 才是真正「记录事件」的探针本体。它的工作方式是 Rust 里非常地道的 **RAII**：

- **创建**一个 `TimingScope` 时，立刻往全局事件缓冲区里 `push` 一条 `Start` 事件；
- 当这个 `TimingScope` 离开作用域被销毁时，Rust 自动调用它的 `Drop::drop`，在里面再 `push` 一条 `End` 事件。

于是，一对 `Start` / `End` 天然包裹住了「从创建到销毁」这段时间——你**永远不需要手写「结束计时」**，只要让作用域自然结束即可（函数返回、块结束、甚至 panic 解栈，都会触发 `Drop`）。

这里有一个**关键且容易忽略的细节**：`TimingScope::new` 和 `with_span` 的返回值不是 `TimingScope`，而是 **`Option<TimingScope>`**。

- 当 `is_enabled()` 为 `true` 时，返回 `Some(scope)`——此时已经记了一条 `Start`，后续销毁时会记 `End`；
- 当 `is_enabled()` 为 `false` 时，返回 `None`——**没有记任何事件，也没有分配、没有加锁**。

这就是「默认关闭、几乎零成本」的直接来源：关闭时你拿到的是一个 `None`，它什么都不做。（关于这套门控的性能剖析，u3-l1 会深入。）

#### 4.3.2 核心流程

一次完整的「创建 → 使用 → 销毁」时间线如下：

```
调用 TimingScope::new("parse")
   │
   ├─ with_span 调用 is_enabled()
   │     │
   │     ├─ false ──► 直接返回 None（不记事件、不加锁、不分配）──► Drop 时也什么都不做
   │     │
   │     └─ true  ──► 调用 new_impl()
   │                     │
   │                     ├─ 取当前线程 id 和时间戳
   │                     ├─ EVENTS.lock().push( Start 事件 )   ← 记下「开始」
   │                     └─ 返回 Some(TimingScope{ ... })
   │
   ▼  （拿到 Some(scope)，被赋给 __scope；之后 body 执行）
   ...
   ▼
离开作用域：__scope 被销毁
   │
   └─ 若是 Some(inner)：触发 TimingScope::drop
         │
         ├─ 取新的时间戳（此刻）
         └─ EVENTS.lock().push( End 事件 )    ← 记下「结束」
```

要点：

- **Start 在 `new_impl` 里记，End 在 `Drop::drop` 里记**，分别在 [src/lib.rs:180-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191) 和 [src/lib.rs:194-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205)。
- 两条事件用的是**同一个** `name` 和 `thread_id`（存在 `TimingScope` 的字段里，创建时确定，销毁时复用），所以它们能被导出工具正确配对。
- 关闭态走的是 `None` 分支，全程不碰 `EVENTS` 这把锁。

#### 4.3.3 源码精读

**结构体定义**——注意三个字段都是创建时就确定、销毁时要复用的：

> [src/lib.rs:152-157](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L152-L157)：`TimingScope` 持有 `name: &'static str`、`span: Option<NonZeroU64>`、`thread_id: u64`。这三个值在创建时写入，`Drop` 时读出来生成对应的 `End` 事件，保证 Start/End 配对一致。

**门控 + 创建（Start 事件）**：

> [src/lib.rs:161-164](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L161-L164)：`new(name)` 直接转调 `with_span(name, None)`——所以「不带 span」就是 span 为 `None` 的特例。
>
> [src/lib.rs:171-177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L171-L177)：`with_span` 先检查 `is_enabled()`：为真才调用 `new_impl` 并包成 `Some`；为假直接返回 `None`。这正是「零成本门控」的入口。
>
> [src/lib.rs:180-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191)：`new_impl` 做三件事——①从线程局部数据取 `thread_id` 和当前时间戳；②给 `EVENTS` 加锁并 `push` 一条 `kind: Start` 的事件；③构造并返回 `TimingScope`。注意它「不检查开关」，因为只有 `with_span` 在开关为真时才会调到它。

**销毁（End 事件）**：

> [src/lib.rs:194-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205)：`Drop for TimingScope` 的实现：取一个**新的**时间戳，再给 `EVENTS` 加锁 `push` 一条 `kind: End` 的事件，`name`/`span`/`thread_id` 都复用 `self` 里存的值。这条 End 与 `new_impl` 里 push 的 Start 天然配对。

> 一个推论：因为 `Drop` 是在持有 `self` 的字段（`name`/`span`/`thread_id`）时执行的，所以即便作用域里发生了 panic 正在解栈，`Drop` 依然会运行并记下 End——计时数据不会因为中途出错而丢失「半截」事件。

**真实使用样例（直接用 TimingScope）**——`typst-syntax` 的解析器没有用 `timed!` 宏，而是直接构造 `TimingScope::new`：

> [crates/typst-syntax/src/parser.rs:15-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L15-L37)：`parse` / `parse_code` / `parse_math` 三个函数，函数体第一行都是 `let _scope = typst_timing::TimingScope::new("parse"/"parse code"/"parse math");`。`_scope` 是 `Option<TimingScope>`，它存活到函数返回，返回时被销毁，于是整个解析过程被一对 Start/End 事件包裹。下划线前缀表示「这个变量我只是为了让它活到函数末尾，不打算读它的值」。

#### 4.3.4 代码实践

这是一个**源码阅读 + 推理型实践**，目标是让你确信「Start/End 确实是在两个不同时机被 push 的」。

1. **实践目标**：说清楚一次 `TimingScope::new("parse")` 调用，分别在「哪一行」产生了 Start 和 End。
2. **操作步骤**：打开 `src/lib.rs`，定位 4.3.3 列出的四处代码（结构体、`new`、`with_span`、`new_impl`、`Drop`）。然后回答下面的问题（口述或写下来）：
   - `Start` 事件是在哪个函数里 `push` 的？那一行同时取了哪两样东西？
   - `End` 事件是在哪个 trait 方法里 `push` 的？它的 `timestamp` 与 Start 的是同一个吗？
3. **需要观察的现象**：Start 与 End 分属两个不同的函数（`new_impl` 与 `drop`），中间隔着「整个被包裹代码的执行」。
4. **预期结果**（参考答案）：
   - Start 在 [src/lib.rs:183-189](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L183-L189) 的 `new_impl` 里 push；同一函数前面还取了 `thread_id` 和 `timestamp`。
   - End 在 [src/lib.rs:196-203](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L196-L203) 的 `Drop::drop` 里 push；它的 `timestamp` 是**重新取的**（`Timestamp::now()`），所以晚于 Start——两者之差就是这段代码的耗时。
5. 本步骤纯阅读推理，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`TimingScope::new("foo")` 的返回类型是什么？为什么不是直接 `TimingScope`？
**答案**：返回 `Option<TimingScope>`（见 [src/lib.rs:162](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L162-L162)）。用 `Option` 是为了在计时关闭时返回 `None`，从而「不构造 `TimingScope`、不记事件、不加锁」——这是零成本门控的关键（u3-l1 详析）。

**练习 2**：如果 `timed!` 包裹的代码在中途 `panic` 了，`End` 事件还会被记录吗？
**答案**：会。Rust 在 panic 解栈时会依次调用所有局部变量的 `Drop`，`__scope`（若是 `Some`）持有的 `TimingScope` 也会被 drop，从而记下 End。所以即使出错，你也能拿到一对完整的 Start/End，便于定位「哪段代码在崩」。

**练习 3**：Start 事件和 End 事件的 `name` / `thread_id` 为什么能保证相同？
**答案**：因为这两个值在 `new_impl` 里被写进了 `TimingScope` 的字段（[src/lib.rs:190](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L190-L190)），`Drop` 时直接从 `self` 读出来复用（[src/lib.rs:200-202](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L200-L202)）。同一个 `TimingScope` 实例的这两个字段不会变，所以配对天然一致。

---

## 5. 综合实践

现在把三个模块串起来：**打开开关 → 用 `timed!` 记一段代码 → 导出 JSON → 肉眼确认一对 B/E 事件**。这是 typst-timing 最短的一条「端到端」使用链，跑通它意味着你已经掌握了本讲的全部内容。

> 提示：本实践沿用 u1-l1 综合实践里那个已引入 typst-timing 的二进制项目；如果你用的是全新项目，请先按 u1-l1 的方式加好依赖（crates.io 写 `typst-timing = "0.15"`，或仓库内写 `path = "../crates/typst-timing"`）。

### 实践目标

验证「`enable()` 后，`timed!` 真的会产生一对可在导出 JSON 中看到的 B/E 事件」，并由此建立「记录 → 导出」的完整直觉。

### 操作步骤

1. 把 `src/main.rs` 改成下面的内容（**示例代码**，由本讲提供）：
   ```rust
   use typst_timing::{enable, export_json, timed};

   fn main() {
       // ① 打开全局计时开关（默认是关的）
       enable();

       // ② 用 timed!（不带 span 形式）包裹一段「耗时代码」
       //    这里用 50ms 休眠模拟一段值得计时的任务
       timed!(
           "sleep",
           std::thread::sleep(std::time::Duration::from_millis(50))
       );

       // ③ 把收集到的事件导出为 Chrome Trace JSON
       //    source 闭包：给定一个 span，返回 (文件名, 行号)
       //    因为我们用的是「不带 span」形式，事件里 span 为 None，闭包不会被调用
       let mut buf: Vec<u8> = Vec::new();
       export_json(&mut buf, |_| ("test.rs".to_string(), 1))
           .expect("export_json 失败");

       // ④ 打印 JSON，肉眼确认其中包含一对 ph:"B" / ph:"E" 事件
       println!("{}", String::from_utf8(buf).expect("非 UTF-8"));
   }
   ```
2. 运行：
   ```bash
   cargo run
   ```

### 需要观察的现象

程序打印一段 JSON 数组，里面恰好有**两个对象**：一个 `"ph":"B"`（begin）、一个 `"ph":"E"`（end），且两者的 `name` 都是 `"sleep"`。

### 预期结果

输出形如（紧凑格式，字段顺序为 `name, cat, ph, ts, pid, tid, args`）：

```json
[
{"name":"sleep","cat":"typst","ph":"B","ts":0.0,"pid":1,"tid":1,"args":null},
{"name":"sleep","cat":"typst","ph":"E","ts":50015.xx,"pid":1,"tid":1,"args":null}
]
```

逐字段对照本讲学过的内容：

| 字段 | 含义 | 对应本讲知识点 |
| --- | --- | --- |
| `name: "sleep"` | 事件名，来自 `timed!` 的第一个参数 | 4.2，`&'static str` |
| `ph: "B"` / `"E"` | Chrome Trace 相位，B=begin、E=end；由 Start/End 事件映射而来 | 4.3，Start→B、End→E |
| `ts: 0.0` 与 `~50000` | 相对于第一条事件的微秒数；End 的 ts - Start 的 ts ≈ 50ms | 4.3，两处分别取的时间戳 |
| `pid: 1` / `tid: 1` | 进程号固定为 1；线程号是本讲 4.3 提到的自定义 u64（主线程通常为 1） | 4.3，`thread_id` 字段 |
| `args: null` | 因为本例没带 span，所以没有 (file, line) 可填 | 4.2，span 可选 |

> 关于 `args: null`：导出时只有当事件带了 span（`event.span` 为 `Some`）才会调用 `source` 闭包生成 `args`；本例用的是不带 span 的 `timed!`，所以 `args` 为 `null`。若你想看到非空的 `args`，可改用带 span 的形式（`timed!("sleep", span = NonZeroU64::new(1).unwrap(), ...)`），届时 `args` 会显示 `{"file":"test.rs","line":1}`——这正是 4.2 所说「span 决定事件是否带源码位置」的体现。
>
> 数值说明：`ts` 的具体尾数取决于你的机器与调度，`tid` 在单线程程序里通常为 1（计数器初值为 1，主线程先取走它）；如果你观察到的 `tid` 或数值与上表略有出入，属正常现象。若整个程序无法编译/运行（如依赖拉取失败），请改用 `path` 引用并标注「待本地验证」，不要假装已跑通。

> 进阶确认（可选）：把导出的 `buf` 写入一个 `events.json` 文件，再打开 [ui.perfetto.dev](https://ui.perfetto.dev) 或浏览器地址栏 `chrome://tracing`，把这个文件拖进去。你会看到时间轴上有一条名为 `sleep`、长约 50ms 的横条——它就是那一对 B/E 事件的可视化。导出格式的每个字段细节会在 **u2-l4** 详讲。

## 6. 本讲小结

- 三个全局开关 `enable()` / `disable()` / `is_enabled()` 操作的是**同一个**静态原子布尔 `ENABLED`（初值 `false`，即默认关闭），三者也只读写这一个变量。
- `timed!` 宏有**两种形式**：带 `span = ...` 展开为 `TimingScope::with_span(name, Some(span))`，不带 span 展开为 `TimingScope::new(name)`；两种都返回被包裹表达式的值，不改变原逻辑。
- `timed!` 的展开顺序是「先建作用域变量、再执行 body」，块结束时变量被销毁——这决定了 Start 一定在 body 之前、End 一定在 body 之后。
- `TimingScope` 用 **RAII** 工作：`new_impl` 里 `push` 一条 `Start` 事件（[src/lib.rs:180-191](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L180-L191)），`Drop::drop` 里再 `push` 一条 `End` 事件（[src/lib.rs:194-205](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L194-L205)）；两者复用同一份 `name`/`span`/`thread_id`，天然配对。
- `TimingScope::new/with_span` 返回的是 **`Option<TimingScope>`**：关闭时返回 `None`（不记事件、不加锁、不分配），这是「默认关闭、几乎零成本」的直接来源。
- 真实埋点随处可见：`typst-syntax` 的 `parse`/`parse_code`/`parse_math` 直接用 `TimingScope::new`（[parser.rs:15-37](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L15-L37)），`typst` 主循环用 `timed!("check stabilized", ...)`（[typst/src/lib.rs:158](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L158-L158)）。

## 7. 下一步学习建议

本讲让你「会用」了 typst-timing：能开关、能埋点、能导出第一份 JSON。但有几个问题我们**刻意绕开了**，它们正是 u2（核心机制）要回答的：

1. **事件到底长什么样？** Start/End 事件结构体有哪些字段？——下一讲 **u2-l1《数据模型：Event 与 EventKind》** 会拆开 `Event` 与 `EventKind`。
2. **事件存放在哪、多线程怎么区分？** `EVENTS` 这个 `Mutex<Vec<Event>>` 和每线程数据是怎么协作的？——**u2-l2《全局状态与线程模型》**。
3. **时间戳是怎么取的、跨平台有何不同？** native 与 wasm 的差异在哪？——**u2-l3《跨平台时间戳：Timestamp 抽象》**。
4. **导出的 JSON 每个字段什么含义？** `export_json` 的 `source` 闭包到底怎么把 span 还原成 (file, line)？——**u2-l4《导出 Chrome Trace JSON》**。

建议的阅读顺序：u2-l1 → u2-l2 → u2-l3 → u2-l4，四篇都是围绕「同一次 `timed!` 调用产生的数据」从不同角度展开，读完你就能从「会用」进阶到「读懂内部」。

如果你更想先搞清「关闭时为什么真的没开销」，可以直接跳到 **u3-l1《零成本与启用门控设计》**，但建议至少先读完 u2-l1，否则 `Option<TimingScope>` 配合 `Event` 的部分会略感跳跃。
