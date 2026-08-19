# LineDiff 的状态模型：游标、行集合与缓冲区

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段说出 `LineDiff` 七个字段的类型、单位和在「字符级差异 → 行级差异」折叠中扮演的角色。
2. 熟练使用 `point_to_offset` / `offset_to_point` 完成 `rope::Point` 与字节偏移的互转，并解释为什么游标推进必须绕道全局字节偏移。
3. 解释 `deleted_rows` / `inserted_rows` 这两个 `BTreeSet` 分别属于哪套行号坐标系、为什么天然有序对后续 `line_operations` 重建至关重要。
4. 用 `is_line_start` / `is_line_end` 把游标位置分成「行首 / 行中 / 行尾」三类，并说出这种分类如何决定后续结算分支的选择。

本讲只回答「状态是什么、每个字段记录什么」；状态如何随 `Insert` / `Delete` / `Keep` 转移（`flush_insert` / `flush_delete` / `trim_buffered_end` 状态机）留给下一讲 u4-l2。本讲会涉及 `flush_delete` 的分支选择，但只以最简单的一条路径为例。

## 2. 前置知识

### 2.1 两种差异语言（回顾 u1-l2）

- `CharOperation`：字符级操作。`Insert { text: String }` 携带新文本；`Delete { bytes }` / `Keep { bytes }` 只带旧文本的字节数。
- `LineOperation`：行级操作，以整行为单位（`u32` 行数），`Insert` 不携带文本——因为折叠发生时新旧文本都已齐备。
- 行是「全有或全无」的单位：一行内哪怕只改了一个字符，行级表示也是「删一行 + 插一行」。

### 2.2 rope::Point：Zed 的二维文本坐标

Zed 的 `rope` crate 用 `Point { row: u32, column: u32 }` 表示文本位置：`row` 是第几行（从 0 开始），`column` 是该行内从行首算起的**字节偏移**。它的 `Debug` 实现打印成 `Point(行:列)`：

- [crates/rope/src/point.rs:L7-L12](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/point.rs#L7-L12)：`Point` 结构体定义，注释说明这是「文本缓冲区中由行和列组成的零基点」。
- [crates/rope/src/point.rs:L14-L18](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/point.rs#L14-L18)：`Debug` 输出格式为 `Point(行:列)`，本讲的推演表格会大量使用这个格式。

两条容易踩坑的语义约定：

1. **`line_len(row)` 包含行尾换行符**。例如 `"aaaa\nbbbb"` 的第 0 行长度是 5（`aaaa\n`），第 1 行长度是 4（`bbbb`）。
2. **换行之后的位置规范化为下一行行首**。字节偏移 5（`aaaa\n` 之后）的规范坐标是 `Point(1:0)`，而不是 `Point(0:5)`。因此「行中间某一行的行尾」在规范坐标里根本不出现——这个推论是 4.4 节的重点。

### 2.3 Point 的加减法是「增量」语义

- [crates/rope/src/point.rs:L74-L84](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/point.rs#L74-L84)：`Point + Point`，当右操作数的 `row == 0` 时只累加列；否则行数相加、**列直接替换**为右操作数的列（右操作数被理解为「跨过 n 个换行后再走 column 列」的位移）。
- [crates/rope/src/point.rs:L94-L106](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/point.rs#L94-L106)：`Point - Point` 得到上述语义下的位移（行数相减、列取被减数的列）。

所以 `b - a` 得到的不是几何向量，而是一段「位移说明」：跨 \(b.row - a.row\) 个换行、再从最后一个换行走 \(b.column\) 列。把它加回 `a` 恰好还原 `b`。

### 2.4 TextSummary 与 BTreeSet

- `TextSummary::from(s).lines` 给出一段字符串的 `Point` 跨度（extent），例如 `TextSummary::from("\ncccc").lines == Point(1:4)`。
- `BTreeSet<u32>` 是有序集合：迭代按升序、自动去重、支持 `peek` 式消费。这两个性质是 4.3 节的主角。

### 2.5 守恒律（回顾 u3-l4）

`StreamingDiff` 产出的操作流满足：`Keep` 与 `Delete` 的字节数之和等于旧文本长度，`Keep` 与 `Insert` 的字节数之和等于新文本长度。`LineDiff` 的两个游标正是这条守恒律的「坐标化进度条」：\(\text{offset}(old\_end) = \text{已 Keep 字节} + \text{已 Delete 字节}\)，\(\text{offset}(new\_end) = \text{已 Keep 字节} + \text{已 Insert 字节}\)（后者针对尚未成形的 new 文本按外推计算）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/streaming_diff/src/streaming_diff.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs) | 本 crate 唯一源文件，`LineDiff`（L288-L522）、`LineOperation`（L281-L286）与全部测试都在这里 |
| [crates/rope/src/rope.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs) | `point_to_offset` / `offset_to_point` / `line_len` / `max_point` 的定义处，理解 `Point` 语义必读 |
| [crates/rope/src/point.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/point.rs) | `Point` 结构体、加减法与 `Debug` 实现 |
| [crates/agent_ui/src/buffer_codegen.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent_ui/src/buffer_codegen.rs) | 真实调用方之一：agent 代码生成界面，每收到一块字符操作就取一次行级操作 |

## 4. 核心概念与源码讲解

### 4.1 LineDiff 总览：一个「累计状态、按需重建」的折叠器

#### 4.1.1 概念说明

`LineDiff` 的工作是把 `StreamingDiff` 产出的字符级操作流「折叠」成行级视图。但它**并不存储** `Vec<LineOperation>`——它存储的是一个足以随时**推导**出行操作的最小状态摘要：

- 两个游标（`old_end` / `new_end`）标记两套坐标系里「处理到哪了」；
- 两个行号集合（`deleted_rows` / `inserted_rows`）标记哪些行被删除 / 插入；
- 两个缓冲（`buffered_insert` / `buffered_delete`）暂存还无法确定行归属的操作；
- 一个标志（`inserted_newline_at_end`）记录「刚在旧文本末尾插出了新行」这一特殊情况。

为什么这样设计？因为使用方式是流式的：调用方每推入一批字符操作，就可能立刻要一次当前的行级视图（`buffer_codegen` 正是这么用的），而此时新旧文本都还不完整。一个「输入累计 + 纯函数输出」的状态机比「边走边改操作列表」更能保证任意时刻的输出都自洽。

#### 4.1.2 核心流程

```text
CharOperation 流（来自 StreamingDiff::push_new / finish）
        │
        ▼
push_char_operation ── 按变体分流（Insert / Delete / Keep 三条路径）
        │                 （转移细节是 u4-l2 的主题）
        ▼
更新状态：游标推进、行号集合插入、缓冲填充/结算
        │
        ▼ （任意时刻、可反复调用）
line_operations(&self) ── 纯读取，从两个有序集合 + 游标归并出 Vec<LineOperation>
```

关键点：`line_operations` 接收 `&self`（不可变借用），它不修改任何状态——输出完全是当前状态的函数。

#### 4.1.3 源码精读

结构体定义（注意源码自带的三条文档注释，它们是理解字段语义的第一手材料）：

- [src/streaming_diff.rs:L288-L302](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L288-L302)：`LineDiff` 的七个字段。`old_end` 注释为「保留与删除文本的 extent」，`new_end` 为「保留与插入文本的 extent」，`deleted_rows` / `inserted_rows` 分别「以旧 / 新文本行号表示」，`buffered_delete` 的注释点明「删除一个换行后缓冲删除，直到保留或插入一个字符」。

逐字段一览（类型、单位、作用）：

| 字段 | 类型 | 坐标系 / 单位 | 记录什么 |
| --- | --- | --- | --- |
| `old_end` | `Point` | 旧文本 | 已被 Keep / Delete 消费到的位置 |
| `new_end` | `Point` | 新文本 | 已被 Keep / Insert 生产到的位置 |
| `deleted_rows` | `BTreeSet<u32>` | 旧文本行号 | 被整体删除或改写的旧行 |
| `inserted_rows` | `BTreeSet<u32>` | 新文本行号 | 插入或改写产生的新行 |
| `buffered_insert` | `String` | 新文本字节 | 尚未结算的插入文本 |
| `buffered_delete` | `usize` | 旧文本字节 | 尚未结算的删除字节数 |
| `inserted_newline_at_end` | `bool` | — | 刚在旧文本末尾插入了换行（长出了新行） |

批量入口与标准用法：

- [src/streaming_diff.rs:L305-L313](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L305-L313)：`push_char_operations` 只是把迭代器里的操作逐个转交给 `push_char_operation`，并透传 `old_text: &Rope`——注意 `LineDiff` 自己不持有旧文本，每次调用都要外部提供。
- [src/streaming_diff.rs:L953-L961](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L953-L961)：测试辅助函数 `char_ops_to_line_ops` 展示了完整生命周期：`LineDiff::default()` → 逐个 `push_char_operation` → `finish(&old_rope)` → `line_operations()`。
- [crates/agent_ui/src/buffer_codegen.rs:L740-L791](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent_ui/src/buffer_codegen.rs#L740-L791)：真实调用方。L740 创建 `LineDiff::default()`，流式循环里每次 `push_char_operations(&char_ops, &selected_text)` 后立刻 `line_diff.line_operations()` 取行级视图发给 UI——印证「随时可读」的设计动机。

#### 4.1.4 代码实践

1. **实践目标**：不借助任何源码改动，从 crate 外部观察到 `LineDiff` 的内部状态变化。
2. **操作步骤**：
   - 字段全部私有，但结构体派生了 `Debug`（L288 的 `#[derive(Debug, Default)]`），所以 `{:?}` 打印就是官方观察窗口。在 `crates/streaming_diff/tests/` 下新建一个集成测试文件 `linediff_trace.rs`（这是新增文件，不触碰库源码；练习完可删除）：

     ```rust
     // 示例代码：crates/streaming_diff/tests/linediff_trace.rs
     use rope::Rope;
     use streaming_diff::{CharOperation, LineDiff};

     #[test]
     fn trace_line_diff_states() {
         let old_text = Rope::from("aaaa\nbbbb\ncccc");
         let mut diff = LineDiff::default();
         println!("init    {:?}", diff);
         for op in [
             CharOperation::Keep { bytes: 5 },
             CharOperation::Delete { bytes: 5 },
             CharOperation::Keep { bytes: 4 },
         ] {
             diff.push_char_operation(&op, &old_text);
             println!("{:<7} {:?}", format!("{:?}", op), diff);
         }
         diff.finish(&old_text);
         println!("finish  {:?}", diff);
         println!("ops     {:?}", diff.line_operations());
     }
     ```

   - 运行：`cargo test -p streaming_diff --test linediff_trace -- --nocapture`（`rope` 在 `[dependencies]` 中，集成测试可直接 `use rope::Rope`，参见 [Cargo.toml:L14-L16](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L14-L16)）。
3. **需要观察的现象**：每条操作之后 `old_end` / `new_end` / `deleted_rows` 的变化，以及 `buffered_delete` 是否在 `Delete` 那一步就被清零。
4. **预期结果**：由源码手工推演（见第 5 节综合实践的完整表格），应依次看到 `old_end: Point(1:0)` → `Point(2:0)`（同时 `deleted_rows: {1}`）→ `Point(2:3)`，`finish` 后 `old_end: Point(2:4)`、`new_end: Point(1:4)`，`line_operations()` 为 `[Keep { lines: 1 }, Delete { lines: 1 }, Keep { lines: 1 }]`。以上为源码推演结论，待本地验证。
5. 若暂时不方便新增文件，也可以直接阅读 [src/streaming_diff.rs:L598-L620](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L598-L620) 的 `test_delete_line_in_middle`，它用的正是同一组输入。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LineDiff` 不直接维护一个 `Vec<LineOperation>`，每来一条字符操作就更新它？

**答案**：流式场景下新旧文本都不完整，行归属（尤其删除换行、插入换行这类边界情况）往往要等到后续的 `Keep` / `Insert` 才能确定；两个游标 + 两个行号集合是比操作序列更稳定的摘要，可去重、可合并，且允许在任意时刻用纯函数 `line_operations()` 重建出当前视图，而不必维护「半成品操作」的修补逻辑。

**练习 2**：`LineDiff::default()` 之后立即调用 `line_operations()`，返回什么？

**答案**：两个集合为空，主循环不执行；随后尾部判断 `old_row(0) < old_end.row + 1(= 1)` 成立，补一个 `Keep { lines: 1 }`（见 [src/streaming_diff.rs:L506-L510](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L506-L510)），返回 `[Keep { lines: 1 }]`。「没有任何差异」也输出一个保底 `Keep`，这与归并算法里的 `cmp::max(1, ...)` 呼应（u4-l3 详述）。

**练习 3**：外部 crate 的代码能直接读取 `diff.old_end` 吗？

**答案**：不能，七个字段都是私有的。外部可见的只有 `Debug` 派生输出和 `push_char_operations` / `push_char_operation` / `finish` / `line_operations` 这几个公开方法。

### 4.2 双游标 old_end 与 new_end：两套坐标系里的进度条

#### 4.2.1 概念说明

`old_end` 与 `new_end` 是同一「消费进度」在两套坐标系里的投影：

- `old_end`（旧文本坐标系）：旧文本中已被 `Keep` 或 `Delete` 消费掉的前缀有多长。
- `new_end`（新文本坐标系）：新文本中已被 `Keep` 或 `Insert` 生产出来的前缀有多长。

对应 u3-l4 的守恒律：\(\text{point\_to\_offset}(old\_end) = \sum \text{Keep 字节} + \sum \text{Delete 字节}\)。当所有操作（含 `finish`）处理完毕时，`old_end` 等于旧文本的 `max_point()`，`new_end` 等于新文本的 extent。

为什么用 `Point` 而不用 `usize` 偏移？因为行折叠关心的恰恰是「位置落在第几行、离行首 / 行尾多远」——`row` 和 `column` 正是这些判定的原料；而纯字节偏移需要每次判定时都做一次换算。

#### 4.2.2 核心流程

四个状态写入点，各自推进哪些游标：

| 写入点 | old_end | new_end |
| --- | --- | --- |
| `keep(bytes)`（保留） | 前进 bytes | 前进相同位移 |
| `flush_delete()`（结算删除） | 前进 buffered_delete | 不动 |
| `flush_insert()`（结算插入） | 不动 | 前进 buffered_insert 的 extent |
| `finish()`（收尾） | 钉到 `max_point()` | 补上与 old_end 的差额 |

游标推进的固定套路是「Point → 偏移 → 加字节 → Point」三步：

```text
target = old_text.offset_to_point( old_text.point_to_offset(old_end) + bytes )
delta  = target - old_end          // 「跨 n 行、末行再走 c 列」的位移
old_end += delta ; new_end += delta   // Add 语义：行数相加、列替换（见 2.3）
```

#### 4.2.3 源码精读

- [src/streaming_diff.rs:L416-L426](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L416-L426)：`keep` 的全部逻辑——`bytes == 0` 早退；否则按上述三步算出位移，**同步**推高两个游标（Keep 意味着新旧文本共同前进），并清掉 `inserted_newline_at_end` 标志。变量名 `lines` 实际存的是 `Point` 位移（行数 + 列数），不是行数。
- [src/streaming_diff.rs:L453-L460](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L453-L460)：`finish` 先清空两个缓冲，然后把 `old_end` 钉到 `max_point()`，`new_end` 加上两者的差额——悬挂的旧文本尾部一律视为 Keep（与 u3-l4 中 `StreamingDiff::finish` 的语义对齐）。
- [src/streaming_diff.rs:L395-L397](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L395-L397)：`flush_delete` 里唯一的 `old_end` 写入，同样是「换算 → 加 buffered_delete → 换算回来」。
- [src/streaming_diff.rs:L360-L362](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L360-L362)：`flush_insert` 用 `TextSummary::from(buffered_insert).lines` 直接得到插入文本的 `Point` 跨度，加到 `new_end` 上——新文本没有 rope，只能靠文本摘要外推。

换算函数本身在 rope crate：

- [crates/rope/src/rope.rs:L455-L464](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L455-L464)：`point_to_offset`，`Point` 超出文本范围时返回文本总长（钳制）。
- [crates/rope/src/rope.rs:L397-L409](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L397-L409)：`offset_to_point`，`offset >= len` 时返回 `summary().lines`（即 `max_point`），同样钳制而不 panic。这意味着即使上游给了超量的 `bytes`，游标也只会停在文本末尾。

#### 4.2.4 代码实践

1. **实践目标**：亲手完成一次游标推进的换算，体会「为什么不能直接 `column += bytes`」。
2. **操作步骤**：取 `old_text = "aaaa\nbbbb\ncccc"`（行 0 长度 5、行 1 长度 5、行 2 长度 4）。设当前 `old_end = Point(1:3)`、`new_end = Point(0:7)`，手算 `keep(4)` 之后两个游标的值。
3. **需要观察的现象**：`point_to_offset(Point(1:3)) = 5 + 3 = 8`；加 4 得 12；`offset_to_point(12)`：字节 10 是行 2 起点，12 落在 `Point(2:2)`。位移 = `Point(2:2) - Point(1:3)` = 行差 1、列取被减数的 2，即 `Point(1:2)`。
4. **预期结果**：`old_end = Point(2:2)`；`new_end += Point(1:2)`，由于位移的行数非 0，按 Add 语义行数加 1、列替换为 2，得 `Point(1:2)`。注意：如果天真地写 `old_end.column += 4`，会得到 `Point(1:7)` 这种跨行错位的结果——这就是三步换算必须存在的原因。本条为纸面推演，可用 4.1.4 的集成测试打印验证（把输入换成 `Keep{8}` 再 `Keep{4}` 即可复现 `Point(1:3)` 起点），待本地验证。
5. 想再验证换算函数本身，可在同一个测试文件里对 `Rope::from("aaaa\nbbbb\ncccc")` 依次打印 `offset_to_point(0/4/5/9/10/14/15)`，应得到 `Point(0:0)/Point(0:4)/Point(1:0)/Point(1:4)/Point(2:0)/Point(2:4)/Point(2:4)`——注意 15（越界）被钳制到 `Point(2:4)`。

#### 4.2.5 小练习与答案

**练习 1**：`old = "aaaa\nbbbb\ncccc"`，`point_to_offset(Point(2:0))` 是多少？

**答案**：10。行 0 占字节 0..5，行 1 占 5..10，行 2 起点是字节 10。

**练习 2**：如果对 9 字节的文本执行 `keep(100)`，会发生什么？

**答案**：`point_to_offset` 得 9，`offset_to_point(109)` 因 `offset >= len` 返回 `max_point`，游标被钉在文本末尾，不 panic。正常调用下 `StreamingDiff` 的守恒律保证 `Keep` 字节数不会越界，这是防御性钳制。

**练习 3**：`keep()` 里为什么必须走 `offset_to_point(point_to_offset(...) + bytes)` 的往返，而不能直接改 `column`？

**答案**：`column` 是行内偏移，加法跨行时结果非法；往返换算让 rope 把「行内偏移越界」规范化成「下一行行首 + 剩余列」。同时差值 `target - old_end` 是位移语义（见 2.3），把它加到 `new_end` 上才能让新侧游标以正确的新文本坐标同步前进。

### 4.3 deleted_rows 与 inserted_rows：两个 BTreeSet 行集合

#### 4.3.1 概念说明

行级差异的最终表达不是逐行的操作列表，而是**两个行号集合**：

- `deleted_rows`：**旧文本**行号集合——这些行被整体删除，或被改写（改写行也在此列）；
- `inserted_rows`：**新文本**行号集合——这些行是插入的产物，或改写后的结果。

关键认知：**两套行号属于不同的坐标系，不能直接比较**。「第 2 个旧行被删」与「第 3 个新行是插入」说的是各自文本里的事。把它们关联起来的任务是 `line_operations()` 的双指针归并（u4-l3 的主题）。

「改写」如何表达？一行内改了几个字符（行中删除 / 行中插入）时，该行的旧行号进 `deleted_rows`，同时对应的新行号进 `inserted_rows`——行级视图里呈现为相邻的 `Delete` + `Insert` 对。这正是 u1-l2「行全有或全无」在数据结构上的落点。

为什么选 `BTreeSet` 而不是 `Vec` 或 `HashSet`？

1. **有序迭代**：归并算法需要按升序 `peek` / `next` 地消费行号；
2. **去重**：写入方是范围 `extend(old_start.row..=old_end.row)`，多次操作的区间可能重叠，集合语义自动合并；
3. **区间语义**：`extend(a..b)` 恰好表达「行 a 到行 b-1 全部标记」。

#### 4.3.2 核心流程

行号集合的写入发生在两个结算函数里。以 `flush_delete` 为例（`flush_insert` 的三分支留待 u4-l2），删除一段旧文本时按位置分三类归属：

```text
删除区间 [old_start, old_end]
├── 起止都在"行尾"              → deleted_rows += old_start.row+1 ..= old_end.row
│                                   （纯尾部整行消失，不影响新行）
├── 行首 → 行首（且未到文本末尾、
│   新侧也停在行首）             → deleted_rows += old_start.row .. old_end.row
│                                   （删掉完整的若干中间行）
└── 其余（触及行中）            → deleted_rows += old_start.row ..= old_end.row
                                    且 inserted_rows.insert(new_end.row)
                                    （旧行被改写，新行是改写产物）
```

本讲只要求看懂第二条路径（整行删除），它正是综合实践中 `Delete{5}` 走的分支。

#### 4.3.3 源码精读

- [src/streaming_diff.rs:L295-L298](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L295-L298)：两个字段的声明与文档注释，明确各自的坐标系（"expressed in terms of the old text" / "the new text"）。
- [src/streaming_diff.rs:L399-L410](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L399-L410)：`flush_delete` 的三分支。第一分支要求起止点都 `is_line_end`；第二分支要求起点 `is_line_start`、终点 `is_line_start` 且未到 `max_point`、且 `new_end.column == 0`；其余落入第三分支，同时写两个集合（改写语义）。L412 顺带清掉 `inserted_newline_at_end`。
- [src/streaming_diff.rs:L464-L467](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L464-L467)：`line_operations` 的开头——把两个集合变成 `peekable` 迭代器，这是 `BTreeSet` 有序性被消费的地方；`old_row` / `new_row` 双计数器同步走两套坐标系。
- [src/streaming_diff.rs:L551-L572](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L551-L572)：`test_delete_second_of_two_lines`。`old = "aaaa\nbbbb"`，操作 `Keep{5}, Delete{4}`：删除发生在最后一行的行中，走第三分支，`deleted_rows = {1}` 且 `inserted_rows = {1}`，最终行操作为 `[Keep{1}, Delete{1}, Insert{1}]`——「删一行插一行」正是改写的行级表达。

#### 4.3.4 代码实践

1. **实践目标**：独立推演一次「行中删除」如何同时写入两个集合。
2. **操作步骤**：对 `test_delete_second_of_two_lines` 的输入（`old = "aaaa\nbbbb"`，操作 `Keep{5}` 后接 `Delete{4}`）逐步写下状态。行 0 长度 5（含换行）、行 1 长度 4、`max_point = Point(1:4)`。
3. **需要观察的现象**：
   - `Keep{5}` 后：`old_end = new_end = Point(1:0)`；
   - `Delete{4}`：`buffered_delete = 4`，`trim_buffered_end` 因 `buffered_insert` 为空返回 0，随后 `flush_delete` 把 `old_end` 推到 `offset_to_point(5 + 4) = Point(1:4)`；
   - 分支判定：起点 `Point(1:0)` 是行首 ✓，但终点 `Point(1:4)` 不是行首（`column != 0`）→ 落入第三分支：`deleted_rows.extend(1..=1)` 得 `{1}`，`inserted_rows.insert(new_end.row = 1)` 得 `{1}`；
   - `finish` 后：`old_end = Point(1:4)`，`new_end = Point(1:0)`（新文本是 `"aaaa\n"`，最后一行是空行）。
4. **预期结果**：最终两个集合均为 `{1}`，`line_operations()` 为 `[Keep{1}, Delete{1}, Insert{1}]`，与测试断言一致；且 `apply_line_operations(old, new, &line_ops) == new` 的 round-trip 成立（新文本 `"aaaa\n"`）。以上为源码推演结论，待本地验证——也可直接在 4.1.4 的测试里换用这组输入打印确认。
5. 思考题（观察用）：为什么这里 `inserted_rows` 插入的新行号 1 对应的是一个**空行**？（提示：新文本 `"aaaa\n"` 以换行结尾，末尾空行是真实存在的行。）

#### 4.3.5 小练习与答案

**练习 1**：`deleted_rows` 里的 `3` 和 `inserted_rows` 里的 `3` 含义相同吗？

**答案**：不同。前者指旧文本第 3 行被删除 / 改写，后者指新文本第 3 行是插入 / 改写产物。两套行号只有在 `line_operations` 的双指针归并中通过 `old_row` / `new_row` 计数器建立对应。

**练习 2**：为什么 `flush_delete` 的第三分支要同时写 `inserted_rows`？

**答案**：删除区间触及行中时，包含删除点的旧行不会干净消失——它的残余会与保留 / 插入的内容拼成新文本中的一个新行，即该行被「改写」。行级语言没有「改半行」，只能同时标记旧行删除、新行插入。

**练习 3**：把 `BTreeSet<u32>` 换成 `HashSet<u32>` 会破坏什么？

**答案**：`line_operations` 依赖 `iter()` 的升序顺序来做 `peek` + `next` 的归并（L464-L467）；`HashSet` 迭代无序，归并前必须显式排序，且无法边消费边 `peek` 最小元素。有序性是该算法的前提。

### 4.4 is_line_start 与 is_line_end：行边界的三分类判定

#### 4.4.1 概念说明

两个模块级的私有自由函数回答「一个位置相对行边界在哪」：

- `is_line_start(point)`：`point.column == 0`——是否停在某行行首；
- `is_line_end(point, text)`：`point.column == text.line_len(point.row)`——是否停在所在行的行尾。

二者组合把位置分成三类，而这三类正是 `flush_insert` / `flush_delete` 选择分支的开关：

| 分类 | 判定 | 直觉含义 |
| --- | --- | --- |
| 行首 | `is_line_start` 且非行尾 | 刚跨过一个换行（或在文本开头） |
| 行尾 | `is_line_end` 且非行首 | 在文本的最末尾（见下文的规范性推论） |
| 行中 | 两者皆否 | 正在某行内部，前后还有同行字符 |

一个重要的**规范性推论**（理解本 crate 分支逻辑的钥匙）：`line_len(row)` 把行尾换行算进行长，而 `offset_to_point` 又把「换行之后」规范化成下一行的 `Point(row+1, 0)`。于是对**规范坐标**而言，中间任何一行的「行尾位置」根本不会被表示出来——它就是下一行的行首。审计 `old_end` 的全部取值来源（`default()` 的 `Point(0:0)`、`offset_to_point` 的换算结果、`finish` 的 `max_point()`，以及由它们 telescoping 相加得到的目标点），全部是规范点。因此在 `old_end` 上：

- `is_line_end` 为真只发生在**文本末尾**（或文本以换行结尾时那个空末行的行首——此时它与 `is_line_start` 同时为真）；
- 「删除起止都在行尾」的第一分支，实际捕捉的是从文本末尾附近开始删除的场景。

顺带定位三个缓冲字段在全局中的角色（细节归 u4-l2）：`buffered_insert` / `buffered_delete` 存「行归属尚未确定」的操作——尤其删除一个换行后，受影响的行是否算整行消失要等下一个 `Keep` 或 `Insert` 才见分晓（L300-L301 的注释原文即此意）；`inserted_newline_at_end` 标记「在旧文本末尾插入了换行、长出了新行」，用来避免把旧的末行误标为删除。

#### 4.4.2 核心流程

```text
is_line_start(p)        ≡  p.column == 0
is_line_end(p, text)    ≡  text.line_len(p.row) == p.column
line_len(row)           ≡  clip_point(Point(row, u32::MAX), Bias::Left).column   // 含行尾换行
```

三分类的使用位置（本讲只看「在哪判定」，不看「判定后做什么」）：

- `Insert` 分支入口 [L320](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L320)：插入发生在行首还是行中，决定缓冲策略；
- `flush_insert` [L364-L371](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L364-L371)：按「旧行首 / 旧行尾 / 行中」三分决定 `inserted_rows` 的写法；
- `Delete` 分支 [L342](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L342) 与 `flush_delete` [L399-L403](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L399-L403)：决定删除的行归属。

#### 4.4.3 源码精读

- [src/streaming_diff.rs:L516-L518](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L516-L518)：`is_line_start`，只需看 `column`，不需要文本。
- [src/streaming_diff.rs:L520-L522](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L520-L522)：`is_line_end`，要与文本核对行长，所以多一个 `text: &Rope` 参数——两个函数签名差异本身就是提示：行首是纯坐标性质，行尾是文本性质。
- [crates/rope/src/rope.rs:L615-L618](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L615-L618)：`line_len` 的实现——把 `Point(row, u32::MAX)` 向左裁剪到该行内，取其 `column`。行长包含行尾换行（对 `"aaaa\nbbbb"` 的行 0 得 5）。
- [crates/rope/src/rope.rs:L324-L326](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs#L324-L326)：`max_point`，文本的最终位置；`flush_delete` 第二分支的 `self.old_end < old_text.max_point()` 依赖 `Point` 按「先行后列」的全序比较。
- [src/streaming_diff.rs:L300-L301](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L300-L301)：`buffered_delete` 的文档注释，说明缓冲的动机——「删除一个换行后，缓冲删除直到保留或插入一个字符」。

#### 4.4.4 代码实践

1. **实践目标**：建立对三分类的手感，并亲眼验证「中间行的行尾在规范坐标中不可见」。
2. **操作步骤**：在 4.1.4 的集成测试里追加一段（`is_line_start` / `is_line_end` 是私有函数，用 `Rope` 的公开 API 复刻判定即可）：

   ```rust
   // 示例代码：追加到 linediff_trace.rs
   #[test]
   fn classify_points() {
       let text = Rope::from("aaaa\nbbbb\ncccc");
       let points = [
           (0u32, 0u32), // 行首（文本开头）
           (0, 4),       // 换行符之前
           (1, 0),       // 换行之后
           (1, 4),       // 第二个换行之前
           (2, 0),       // 末行行首
           (2, 4),       // 文本末尾
       ];
       for (row, column) in points {
           let is_start = column == 0;
           let is_end = text.line_len(row) == column;
           println!("Point({row}:{column}) start={is_start} end={is_end}");
       }
   }
   ```

3. **需要观察的现象**：`line_len` 的返回值（5、5、4），以及哪些点同时满足 / 不满足两个判定。
4. **预期结果**：`Point(0:0)` 行首；`Point(0:4)`、`Point(1:4)` 两者皆否（行中，虽然它们紧贴换行）；`Point(1:0)`、`Point(2:0)` 行首；只有 `Point(2:4)`（`max_point`）是行尾。没有任何中间点能成为行尾——印证 4.4.1 的规范性推论。待本地验证。
5. 追加思考：若文本是 `"aaaa\n"`（以换行结尾），`max_point = Point(1:0)` 且 `line_len(1) == 0`，此时 `Point(1:0)` **既是行首又是行尾**——这是两个判定同时为真的唯一常见情形。

#### 4.4.5 小练习与答案

**练习 1**：对 `"aaaa\nbbbb\ncccc"`，`Point(1:4)` 是行首、行中还是行尾？

**答案**：行中。`column = 4 ≠ 0` 不是行首；`line_len(1) = 5 ≠ 4` 不是行尾。它只是第二个换行符之前的位置。

**练习 2**：为什么删除一段「起止都在行中间」的文本会落入 `flush_delete` 的第三分支，而「行首到行首」的删除可以走第二分支？

**答案**：第二分支的删除区间恰好覆盖若干完整行（从某行行首到另一行行首），新文本中不需要任何行来承接残余，所以只写 `deleted_rows`；行中删除会拆散一个旧行，残余必须拼进新文本的某一行，因此还要把 `new_end.row` 标记为插入行（改写语义）。

**练习 3**：`is_line_start` 不需要 `&Rope` 参数，`is_line_end` 需要。为什么？

**答案**：行首只取决于坐标本身（`column == 0`）；行尾取决于该行实际有多长，必须查询文本的 `line_len`。

## 5. 综合实践

把本讲四个模块串起来：**手工模拟 `LineDiff` 处理 `Keep{5}, Delete{5}, Keep{4}`（`old = "aaaa\nbbbb\ncccc"`）的完整状态轨迹，再用本地运行验证。** 这组输入正是 [src/streaming_diff.rs:L598-L620](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L598-L620) `test_delete_line_in_middle` 使用的用例。

### 5.1 地基数据

`old = "aaaa\nbbbb\ncccc"`，共 15 字节、3 行：

| 行 | 内容（含换行） | 字节区间 | `line_len` |
| --- | --- | --- | --- |
| 0 | `aaaa\n` | 0..5 | 5 |
| 1 | `bbbb\n` | 5..10 | 5 |
| 2 | `cccc` | 10..15 | 4 |

`max_point = Point(2:4)`；关键换算：`offset_to_point(5) = Point(1:0)`、`offset_to_point(10) = Point(2:0)`、`offset_to_point(14) = Point(2:3)`、`offset_to_point(15) = Point(2:4)`。

### 5.2 手推状态表（预期答案）

| 事件 | old_end | new_end | deleted_rows | inserted_rows | buffered_insert | buffered_delete |
| --- | --- | --- | --- | --- | --- | --- |
| 初始 | `Point(0:0)` | `Point(0:0)` | `{}` | `{}` | `""` | 0 |
| ① `Keep{5}` | `Point(1:0)` | `Point(1:0)` | `{}` | `{}` | `""` | 0 |
| ② `Delete{5}` 进入缓冲 | `Point(1:0)` | `Point(1:0)` | `{}` | `{}` | `""` | 5 |
| ③ `Delete` 结算后 | `Point(2:0)` | `Point(1:0)` | `{1}` | `{}` | `""` | 0 |
| ④ `Keep{4}` | `Point(2:3)` | `Point(1:3)` | `{1}` | `{}` | `""` | 0 |
| ⑤ `finish()` | `Point(2:4)` | `Point(1:4)` | `{1}` | `{}` | `""` | 0 |

推演要点（对照源码逐步核验）：

1. **①**：`keep(5)` 三步换算——`point_to_offset(Point(0:0)) = 0`，加 5 得 5，`offset_to_point(5) = Point(1:0)`；位移 `Point(1:0)` 同步加到两个游标（[L416-L426](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L416-L426)）。
2. **②→③**：`Delete` 分支先 `buffered_delete += 5`（[L337](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L337)）；`trim_buffered_end` 因 `buffered_insert` 为空返回 0（[L428-L451](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L428-L451)）；`is_line_end(Point(1:0))` 为假（`line_len(1)=5≠0`），于是进入 `flush_delete`（[L342-L345](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L342-L345)）：`old_end` 推进到 `offset_to_point(5+5) = Point(2:0)`；起点 `Point(1:0)` 是行首、终点 `Point(2:0)` 是行首且小于 `max_point`、`new_end.column == 0`——**第二分支**成立，`deleted_rows.extend(1..2)` 得 `{1}`（[L402-L406](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L402-L406)）。语义：从第 1 行行首删到第 2 行行首 = 整行删除 `"bbbb\n"`。
3. **④**：`keep(4)`——偏移 10 + 4 = 14 → `Point(2:3)`，位移 `Point(0:3)` 加到两游标，`new_end` 从 `Point(1:0)` 到 `Point(1:3)`。
4. **⑤**：`finish()` 把 `old_end` 钉到 `max_point = Point(2:4)`，`new_end` 补上差额 `Point(0:1)` 得 `Point(1:4)`（[L453-L460](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L453-L460)）。此时新文本是 `"aaaa\ncccc"`（2 行），`new_end = Point(1:4)` 与之自洽。

最终 `line_operations()`：双指针归并 `deleted_rows={1}`、`inserted_rows={}` 得 `[Keep{1}, Delete{1}, Keep{1}]`，与测试断言一致；守恒检查：`Keep + Delete 行数 = 1 + 1 + 1 = 3 = 旧行数`，`Keep + Insert 行数 = 2 = 新行数`。

### 5.3 验证方式（两条路线）

**路线 A（推荐，零源码改动）**：运行 4.1.4 的集成测试 `linediff_trace`（`cargo test -p streaming_diff --test linediff_trace -- --nocapture`）。派生 `Debug` 会按字段声明顺序打印整行，例如 ④ 之后应为：

```text
LineDiff { inserted_newline_at_end: false, old_end: Point(2:3), new_end: Point(1:3), deleted_rows: {1}, inserted_rows: {}, buffered_insert: "", buffered_delete: 0 }
```

与 5.2 表格逐行对照。以上输出为源码推演结论，待本地验证。

**路线 B（深入内部，需复制源码）**：`flush_delete` / `keep` 都是私有函数且字段私有，想在每个写入点打点，需把 `src/streaming_diff.rs` 复制到一个独立练习 crate（依赖 `ordered-float` 与指向本仓库的 `rope` path 依赖，参见 [Cargo.toml:L14-L16](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/Cargo.toml#L14-L16)），在 `flush_delete` 的分支判定前后、`keep` 的换算处插入 `eprintln!` 打印 `old_start` / `self.old_end` / `new_end`，直接观察 ②→③ 之间「进入缓冲 → 三分支判定 → 集合写入」的中间态。练习完删除副本，不要改动仓库源码。

### 5.4 检查点问题

完成后自测（答案都在 5.2 的推演里）：

1. 被删除的 5 个字节对应旧文本的哪个区间？为什么它恰好是「第 1 行整行」？（字节 5..10 = `bbbb\n`，行首到行首。）
2. 为什么 ④ 之后 `old_end` 停在 `Point(2:3)` 而不是 `Point(2:4)`？`finish` 又把它推到了哪、推了多少？（`Keep{4}` 只消费 4 字节；`finish` 补齐悬挂尾部 1 字节。）
3. 整个过程中 `inserted_rows` 为什么始终为空？（本例是纯整行删除，没有任何改写或插入。）

## 6. 本讲小结

- `LineDiff` 是「累计状态、按需重建」的折叠器：不存储行操作，只维护 2 个游标 + 2 个行号集合 + 2 个缓冲 + 1 个标志，`line_operations(&self)` 随时可从状态纯函数地重建出行操作序列。
- `old_end` / `new_end` 分别是旧、新两套坐标系里的消费进度条，对应守恒律 \(\text{offset}(old\_end) = \text{已Keep} + \text{已Delete}\)；`keep` 同步推双游标，`flush_delete` / `flush_insert` 各推一侧，`finish` 把 `old_end` 钉到 `max_point`。
- 游标推进必须走 `offset_to_point(point_to_offset(p) + bytes)` 往返：`column` 是行内偏移，跨行加法非法；rope 的换算还带越界钳制（钉到 `max_point`，不 panic）。
- `deleted_rows`（旧行号）与 `inserted_rows`（新行号）是两套不可直接比较的坐标系；「改写一行」= 旧行号进删除集合且新行号进插入集合；`BTreeSet` 的有序 + 去重是归并重建算法的前提。
- `is_line_start`（`column == 0`）与 `is_line_end`（`column == line_len(row)`）把位置分成行首 / 行中 / 行尾三类；由于 `line_len` 含换行且换行后位置规范化为下一行行首，规范坐标下 `is_line_end` 只在文本末尾为真。
- 字段私有意味着外部观察窗口只有派生 `Debug` 与四个公开方法——这既是本讲实践的手段，也是理解封装边界的素材。

## 7. 下一步学习建议

下一讲 **u4-l2「字符操作到行边界：flush 与 trim 的状态机」** 将打开本讲刻意留在门外的转移逻辑：`push_char_operation` 三个分支的完整分流、`flush_insert` 的三分支行归属规则、`trim_buffered_end` 如何把「先插后删」的公共后缀折叠成 `Keep`。建议带着本讲的两个问题去读：

1. `buffered_delete` 为什么要等到 `Keep` 或 `Insert` 才结算（提示：删除一个换行后，行归属未定）？
2. `inserted_newline_at_end` 在 `flush_insert` 的行尾分支里如何阻止旧末行被误标为删除？

之后 u4-l3 会拆解 `line_operations` 的双指针归并细节（`cmp::max(1, min(...))` 保底 `Keep` 的用意），u4-l5 回到 `agent` / `agent_ui` 的真实调用现场。若想巩固 rope 坐标系，可顺带阅读 [crates/rope/src/rope.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs) 中 `clip_point` 与 `TextSummary` 的相关实现。
