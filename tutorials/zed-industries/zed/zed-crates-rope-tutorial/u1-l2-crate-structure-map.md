# 源码地图：六个文件如何分工

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `rope` crate 六个源文件各自的职责边界：`rope.rs`（crate 根与导出）、`chunk.rs`（定长块与位图）、`point.rs`、`point_utf16.rs`、`offset_utf16.rs`（三套坐标类型）和 `unclipped.rs`（未裁剪坐标包装器）。
2. 画出（或默写出）这六个文件之间「谁 `use` 谁」的模块依赖图，并解释为什么模块之间允许互相引用。
3. 列举 crate 对外导出的全部类型：`pub use` 再导出的 7 个类型，加上 `rope.rs` 中直接 `pub` 定义的 11 个类型，并知道哪些东西被刻意留在 crate 内部（如 `Bitmap`、`MIN_BASE`）。
4. 通过一个外部小程序验证每个公开类型都能被 `use rope::{...}` 引入，并为每个类型写出一句话职责。

本讲是纯「读地图」的一讲：不深入任何算法，只把代码的居住地搞清楚。后续所有讲义都会不断引用本讲建立的文件地图。

## 2. 前置知识

### 2.1 Rust 模块系统速成：`mod`、`pub use` 与 crate 根

Rust 的每个 crate 有一个**crate 根**（库 crate 通常是 `lib.rs`）。crate 根里用 `mod xxx;` 声明子模块，编译器会去找 `src/xxx.rs`。要点：

- `mod xxx;` 声明的模块**默认是私有的**：外部代码不能写 `rope::xxx::SomeType` 这样的路径。
- 子模块里的类型想暴露给外部，有两条路：把模块本身声明为 `pub mod`，或者在 crate 根用 `pub use xxx::SomeType;` **再导出**（re-export）。
- 再导出是一种「门面」（facade）手法：不管内部拆了多少文件，对外只呈现一个扁平的命名空间——用户永远写 `rope::Point`，而不是 `rope::point::Point`。
- 同一个 crate 内部，模块之间用 `crate::` 路径互相引用（例如 `crate::chunk::Bitmap`）。**crate 内的模块允许互相引用甚至循环引用**（A 用 B、B 用 A），这与「crate 之间禁止循环依赖」是两回事。

### 2.2 没有 `lib.rs` 的 crate：`[lib] path`

rope 没有 `lib.rs`。上一讲我们看过 [crates/rope/Cargo.toml:11-12](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/Cargo.toml#L11-L12)：

```toml
[lib]
path = "src/rope.rs"
```

这是 Zed 仓库的编码规范（见仓库 CLAUDE.md：「用 `[lib] path` 指定库根，保持文件名语义化」）。效果是：**`src/rope.rs` 同时扮演 `lib.rs` 的角色**——它既是模块声明中心，又装着最重要的类型 `Rope` 本体。读这个 crate 时，`rope.rs` 就是你的起点兼「总目录」。

### 2.3 上一讲回顾

u1-l1 已建立的核心认知，本讲直接沿用：

- `Rope` 只有一个字段 `chunks: SumTree<Chunk>`，即「前缀和树上挂着一个个文本块」。
- `len()` 是 UTF-8 **字节数**，字符数要看 `summary().chars`。
- `Point` 的 `column` 按字节计（UTF-8 口径）。
- 依赖分工：`sum_tree` 提供树骨架，`heapless` 提供块内的定长缓冲，`rayon` 服务大文本并行分块，`unicode-segmentation` 处理字素（grapheme）边界。

### 2.4 先建立直觉：编辑器为什么需要「好几套坐标」

本讲要介绍 4 个坐标相关的小文件。先给一个生活化的直觉，细节留到 u2-l1 展开：

- 你自己数文本位置时，习惯用「第几行、第几列」——这是 `Point`。
- JavaScript / LSP 协议数位置时，用的是 **UTF-16 code unit**（因为 JS 字符串是 UTF-16）：一维的字数是 `OffsetUtf16`，二维的行列是 `PointUtf16`。
- Rust 内部数位置时，用的是 **UTF-8 字节偏移**（`usize`）——rope 里所有操作的基准坐标。
- 有些外部坐标可能落在「半个字符」上（比如把光标放在一个 emoji 的中间两字节之间），这类「还没校准过的脏坐标」用 `Unclipped<T>` 打包标记。

于是 rope 的文件分工自然浮现：**存文本的**（rope.rs、chunk.rs）和**数位置的**（point.rs、point_utf16.rs、offset_utf16.rs、unclipped.rs）。

## 3. 本讲源码地图

本讲覆盖 `crates/rope` 下全部 6 个源文件（另加 `Cargo.toml` 作为入口佐证）：

| 文件 | 大约行数 | 职责一句话 | 依赖谁（crate 内） |
| --- | --- | --- | --- |
| `src/rope.rs` | ~2500 | crate 根：声明全部子模块并再导出；定义 `Rope`、游标 `Cursor`、迭代器 `Chunks`/`Bytes`/`Lines`、摘要 `TextSummary`/`ChunkSummary`、trait `TextDimension`；尾部是 ~800 行测试 | `chunk`（Bitmap、MIN_BASE、MAX_BASE） |
| `src/chunk.rs` | ~800 | 定长文本块 `Chunk`（最长 128 字节 + 四张位图）、零拷贝切片 `ChunkSlice`、块内统计与坐标换算、Tab 迭代器 | crate 根（`TextSummary` 等）、外部 `util` |
| `src/point.rs` | ~147 | UTF-8 口径的行列坐标 `Point`：字段、算术、比较 | 无（只依赖 std） |
| `src/point_utf16.rs` | ~120 | UTF-16 口径的行列坐标 `PointUtf16`，结构几乎与 `Point` 同构 | 无（只依赖 std） |
| `src/offset_utf16.rs` | ~50 | UTF-16 口径的一维偏移 `OffsetUtf16`（newtype） | 无（只依赖 std） |
| `src/unclipped.rs` | ~52 | 泛型包装器 `Unclipped<T>`：标记「未裁剪到合法边界」的坐标 | crate 根（`ChunkSummary`） |

「大约行数」按当前 HEAD 的文件字节数估算，仅供建立体感：**rope.rs 独占全 crate 三分之二以上的体量，其余五个文件都很薄**。三个坐标文件加起来不到 350 行，却支撑了整个 crate 的查询能力。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：4.1 crate 根 `rope.rs`；4.2 `chunk.rs`；4.3 三个坐标叶子模块；4.4 `unclipped.rs`；4.5 全景依赖图与导出面清单。

### 4.1 rope.rs：crate 根、门面与导出面

#### 4.1.1 概念说明

`rope.rs` 是这个 crate 的「总机房」：对内它声明所有子模块、集中定义与树交互的类型；对外它通过 `pub use` 把散在子文件里的类型汇聚成一个扁平命名空间。理解一个陌生 crate 的最快路径就是读它的 crate 根开头——那里写明了「这个 crate 由哪几块组成、对外暴露什么」。

#### 4.1.2 核心流程

读 crate 根的固定套路：

1. 看 `[lib] path`（这里是 `src/rope.rs`）确认库根文件。
2. 读文件开头的 `mod` 声明 → 得到内部模块清单。
3. 读 `pub use` → 得到再导出的公开类型。
4. 读 `use crate::xxx::YYY` → 得知根模块直接消费了子模块的哪些**内部**实现细节。
5. 扫一遍 `pub struct` / `pub trait` 的分布 → 得知哪些大类型直接住在根里。
6. 看文件尾部的 `#[cfg(test)] mod tests` → 测试也集中在这里。

#### 4.1.3 源码精读

**模块声明与再导出**——整个 crate 的骨架就这 23 行：

[crates/rope/src/rope.rs:1-23](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1-L23) —— 第 1–5 行按字母序声明 5 个**私有**子模块；第 7–15 行引入外部依赖（`heapless` 的 `Vec` 在这里被别名为 `ArrayVec`，`sum_tree` 引入了 `Bias`/`Dimension`/`Dimensions`/`SumTree` 四个名字）；第 17–21 行 `pub use` 把 7 个类型（`Chunk`、`ChunkSlice`、`OffsetUtf16`、`Point`、`PointUtf16`、`Unclipped`）再导出为公开 API；第 23 行 `use crate::chunk::Bitmap` 则是**内部消费**——位图类型没有出现在 `pub use` 里。

注意一个细节：模块是私有的（`mod chunk;` 而非 `pub mod chunk;`），所以外部代码**不能**写 `rope::chunk::Chunk`，只能写 `rope::Chunk`。这是刻意收窄的 API 面。

**`Rope` 本体**住在根里：

[crates/rope/src/rope.rs:25-28](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L25-L28) —— `Rope` 结构体只有 `chunks: SumTree<Chunk>` 一个字段（u1-l1 已精读）。

**根里还住着哪些公开类型？**（行号为当前 HEAD 实测）

| 类型 | 行号 | 一句话职责 |
| --- | --- | --- |
| `Rope` | [rope.rs:26](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L26) | 文本本体：树 + 块 |
| `Cursor<'a>` | [rope.rs:678-693](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L678-L693) | 只进不退的定位游标：`seek_forward`/`slice`/`summary`/`suffix` |
| `ChunkBitmaps<'a>` | [rope.rs:786-795](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L786-L795) | 「文本 + 三张位图」的只读视图（`text`/`chars`/`tabs`/`newlines` 公开字段） |
| `Chunks<'a>` | [rope.rs:798-825](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L798-L825) | 块迭代器：支持正/反向、区间限定、`next_line`/`prev_line`、`equals_str` |
| `ChunkWithBitmaps<'a>` | [rope.rs:1062](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1062) | 包一层 `Chunks`，让 `Iterator` 吐出 `ChunkBitmaps` 而非裸 `&str` |
| `Bytes<'a>` | [rope.rs:1107-1128](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1107-L1128) | 字节迭代器，还实现了 `io::Read`（[rope.rs:1159](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1159)），方便流式写出文件 |
| `Lines<'a>` | [rope.rs:1186-1194](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1186-L1194) | 按行迭代的适配器（由 `Chunks::lines` 产出） |
| `ChunkSummary` | [rope.rs:1265-1278](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1265-L1278) | 单个块的摘要，内部就是包了一层 `TextSummary`，是挂到 `sum_tree` 上的那个 Summary |
| `TextSummary` | [rope.rs:1280-1304](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1280-L1304) | 一段文本的全部统计量（9 个字段），u2-l2 精读 |
| `TextDimension` | [rope.rs:1441-1447](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1441-L1447) | trait：「可以按某种坐标度量文本」的抽象，u2-l3 精读 |
| `DimensionPair<K, V>` | [rope.rs:1591-1597](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1591-L1597) | 一次查找同时携带两种度量的组合维度（只用 `key` 比较，`value` 跟着累计） |

**测试也在这个文件里**：

[crates/rope/src/rope.rs:1727-1738](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1727-L1738) —— `#[cfg(test)] mod tests` 从这里开始直到文件结尾（~800 行），用 `#[ctor]` 初始化测试日志，大量使用 `#[gpui::test(iterations = 100)]` 参数化随机测试（u3-l2 精读）。注意 [rope.rs:2509-2510](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L2509-L2510) 在测试模块里给 `Rope` 补了一个 `fn text(&self) -> String`——这就是测试代码里 `rope.text()` 的来源，**它不是公开 API**，别在测试之外找它。

#### 4.1.4 代码实践：用 rustdoc 直接「数出」导出面

纸上谈兵容易漏，让编译器替我们列清单：

1. **实践目标**：不写一行代码，拿到 rope 全部公开项的权威清单。
2. **操作步骤**：在 Zed 仓库根目录运行：

   ```bash
   cargo doc -p rope --no-deps
   ```

   然后用浏览器打开 `target/doc/rope/index.html`（也可以加 `--open` 直接打开）。
3. **需要观察的现象**：左侧栏 Structs 列表中出现 `Rope`、`Cursor`、`Chunks`、`Bytes`、`Lines`、`TextSummary`、`ChunkSummary`、`ChunkBitmaps`、`ChunkWithBitmaps`、`DimensionPair`、`Point`、`PointUtf16`、`OffsetUtf16`、`Unclipped`、`Chunk`、`ChunkSlice`；Traits 中出现 `TextDimension`。
4. **预期结果**：清单与 4.1.3 的表格一致；你**找不到** `Bitmap`、`MIN_BASE`、`MAX_BASE`——它们是 `pub(crate)`，rustdoc 不会展示。首次生成文档需要编译依赖，耗时几分钟属正常。

#### 4.1.5 小练习与答案

**练习 1**：外部代码写 `use rope::chunk::Chunk;` 能编译通过吗？为什么？

答案：不能。`mod chunk;` 是私有模块声明（rope.rs:1），路径 `rope::chunk::` 对外不可见；正确写法是 `use rope::Chunk;`，走 crate 根的 `pub use`（rope.rs:17）。

**练习 2**：`ChunkBitmaps` 的 `chars` 字段是 `pub` 的，但为什么说「位图类型没有导出」？这两句话矛盾吗？

答案：不矛盾。字段 `pub` 意味着拿到 `ChunkBitmaps` 值的人可以读它；但字段的**类型** `Bitmap` 是 `pub(crate)` 别名（chunk.rs:9），外部无法写出 `rope::Bitmap` 这个名字。由于类型别名本质是透明的，外部读到的值可以直接当 `u128` 用（例如调 `.count_ones()`），只是不能按原名引用它。这是「值可用、名不可用」的中间态设计。

**练习 3**：为什么 `Cursor`、`Chunks` 这些迭代器类型定义在 `rope.rs` 根里，而 `Chunk` 定义在 `chunk.rs` 里？

答案：迭代器都是「绑着整棵 `Rope`（`&'a Rope`）」的类型，与 `Rope` 的私有字段 `chunks` 紧密协作，放在根里访问私有字段最自然；`Chunk` 是自成一体的数据结构（定长缓冲 + 位图），不依赖 `Rope` 存在，独立成文件便于单独理解与测试。这是一种按「内聚性」切分文件的典型手法。

### 4.2 chunk.rs：定长块与四张位图

#### 4.2.1 概念说明

`Chunk` 是 rope 真正存字节的地方：一块不超过 128 字节的文本，外加四张 u128 **位图**（bitmap）——可以理解为四个和文本等长的「涂改条」，第 i 位置 1 表示「文本第 i 个字节处发生了某件事」（是字符起始 / 需要算两个 UTF-16 单元 / 是换行 / 是制表符）。有了位图，块内的「数行数、数字符、找字符边界」都变成一次位运算，而不用逐字节扫描。`ChunkSlice<'a>` 则是对块内一段文本的**零拷贝**借用视图。本讲只认门脸，位图细节留给 u2-l4。

#### 4.2.2 核心流程

`Chunk` 在整个 crate 里的生命周期：

1. **诞生**：`Rope::push` 把输入文本按 ≤128 字节切段，每段 `Chunk::new(text)` 构造（rope.rs:199）。
2. **上树**：块作为 `sum_tree` 的 Item 挂上树；挂树时调用 `summary()` 算出自己的 `ChunkSummary`（rope.rs:1255-1263）。
3. **被查**：树按字节偏移定位到某个块后，块内的精确位置（第几行、第几个 UTF-16 单元）由 `ChunkSlice` 的位图运算完成。
4. **分裂/合并**：编辑发生时块被 `split_at` 切开或 `append`/`prepend` 拼接，位图跟着做移位同步。

#### 4.2.3 源码精读

[crates/rope/src/chunk.rs:1-6](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L1-L6) —— chunk.rs 的 import：注意它反向引用了 crate 根（`crate::{OffsetUtf16, Point, PointUtf16, TextSummary, Unclipped}`），还引入外部的 `heapless::String`（别名 `ArrayString`）、`sum_tree::Bias`、`unicode_segmentation::GraphemeCursor` 和 `util::debug_panic`。这说明 chunk.rs 是「消费」坐标类型和摘要类型的一方。

[crates/rope/src/chunk.rs:8-14](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L8-L14) —— 位图类型别名与两个关键常量：生产环境 `Bitmap = u128`（因此 `MAX_BASE = 128`、`MIN_BASE = 64`）；测试环境把位图换成 `u16`，于是块上限自动缩成 16 字节——**用更小的块跑同样的逻辑，让边界情况更容易被测试触发**。三个名字都是 `pub(crate)`，不对外。

[crates/rope/src/chunk.rs:16-33](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L16-L33) —— `Chunk` 结构体：四个位图字段 + `pub text: ArrayString<MAX_BASE, u8>`（heapless 的定长字符串，容量 128 字节、不做堆分配）。字段注释精确描述了每张位图的置位规则，其中 `chars_utf16` 的注释解释了「4 字节 emoji 占两个 UTF-16 单元 → 连续两位置 1」。

[crates/rope/src/chunk.rs:49-102](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L49-L102) —— `Chunk::new`：按 8 字节一组循环构造位图（先攒成字节数组再 `Bitmap::from_le_bytes` 拼成 u128），其中 `chars_utf16` 用了 `(bitmap << 1) | chars` 的位技巧。本讲只需知道「构造即完成全部统计预处理」。

[crates/rope/src/chunk.rs:167-179](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L167-L179) —— 对外读取位图的三个方法 `chars()`/`tabs()`/`newlines()`：注意没有 `chars_utf16()` 的读取器，那张位图只在内部分析时用。

[crates/rope/src/chunk.rs:218-236](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L218-L236) —— `ChunkSlice<'a>` 结构体（字段私有）和 `impl Into<Chunk> for ChunkSlice`：切片可以转回_owned_ 的 `Chunk`（`try_into().unwrap()` 依赖「块长 ≤ 128」这一不变量）。

[crates/rope/src/chunk.rs:317-322](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L317-L322) —— `ChunkSlice::text_summary`：块级的统计入口，`Rope` 侧的 `TextSummary` 组装从这里取数。

[crates/rope/src/chunk.rs:677-696](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L677-L696) —— 文件尾部还有一组面向 tab 的类型：`Tabs` 迭代器与 `TabPosition`（给出每个制表符的字节偏移与列位置），服务于编辑器的 tab 展开。

[crates/rope/src/chunk.rs:737-760](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L737-L760) —— 私有函数 `panic_char_boundary` / `log_err_char_boundary`：UTF-8 边界违规的两种处理（panic 或记日志），u3-l1 精读。它们只在 chunk.rs 内部使用，也体现了「错误策略贴近出错现场」的分层。

#### 4.2.4 代码实践：亲手构造一个 Chunk 并读它的位图

`Chunk::new` 和三个位图读取器都是 `pub` 的，可以在 Zed 仓库内用一个现成测试来观察它们。最快的验证路径是跑既有测试：

1. **实践目标**：确认「位图的置位数 = 字符数」这一最基本的事实。
2. **操作步骤**：在仓库根目录运行：

   ```bash
   cargo test -p rope test_all_4_byte_chars -- --nocapture
   ```

   该测试位于 [rope.rs:1740-1746](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1740-L1746)，把 256 个篮球 emoji（每个 4 字节）push 进 Rope 再完整读回比对。
3. **需要观察的现象**：测试通过，说明 128 字节块被连续的 4 字节字符填满、切分后仍能无损还原文本。
4. **预期结果**：`test test_all_4_byte_chars ... ok`。（想直观看到位图值，可使用第 5 节综合实践里的外部小程序打印 `chunk.chars().count_ones()` 与 `chunk.text.len()` 的差别。）

#### 4.2.5 小练习与答案

**练习 1**：一个 `Chunk` 里装着 `"ab\ncd"`，`chars()` 位图有几个置位？`newlines()` 呢？

答案：`chars()` 有 5 个置位（5 个 ASCII 字符各占 1 个起始位）；`newlines()` 只有 1 个置位（下标 2）。注意所有字符都是单字节 ASCII 时 `chars` 位图全 1，一旦出现多字节字符，其后续字节对应的位是 0。

**练习 2**：为什么测试配置要把 `Bitmap` 从 `u128` 换成 `u16`？

答案：块容量 `MAX_BASE = Bitmap::BITS`（chunk.rs:14），测试下自动变成 16。随机测试里的文本更容易跨很多块，从而高频触发「块分裂、块合并、跨块边界」这些最容易出 bug 的路径；同时 `MIN_BASE` 也随之缩为 8，让「不满半块」的分支同样被覆盖。

**练习 3**：`Chunk` 与 `ChunkSlice` 的所有权关系是什么？为什么 `Rope` 树上存的是前者？

答案：`Chunk` 拥有自己的定长缓冲（`ArrayString`），`ChunkSlice<'a>` 只借用 `&'a str` 并复制四张位图（Copy 类型）。树需要长期持有数据所以存 owned 的 `Chunk`；切分、裁剪等临时操作用 `ChunkSlice` 避免拷贝。

### 4.3 point.rs / point_utf16.rs / offset_utf16.rs：三套坐标的叶子模块

#### 4.3.1 概念说明

这三个文件是 crate 里的「叶子模块」：**只 `use` std，不被 crate 内其他模块依赖（除了被根模块再导出和被 chunk.rs 消费）**，每个文件一个类型，纯粹表达「位置」的数学。它们的共同任务是把「编辑器/协议里的位置」变成可做加法、可做比较的值类型，并且都能充当 `sum_tree` 的**维度**（Dimension）——树才能按它们做前缀和查找（`Dimension` 的 impl 写在 rope.rs，本讲不展开）。

三套坐标的对应关系：

| 类型 | 文件 | 维度 | 列/单位口径 | 典型来源 |
| --- | --- | --- | --- | --- |
| `usize`（无独立文件） | — | 一维 | UTF-8 字节 | rope 内部基准 |
| `Point` | point.rs | 二维 | 行 + **字节**列 | rope 自身、显示定位 |
| `OffsetUtf16` | offset_utf16.rs | 一维 | UTF-16 code unit | LSP 等协议 |
| `PointUtf16` | point_utf16.rs | 二维 | 行 + **UTF-16** 列 | LSP 等协议 |

#### 4.3.2 核心流程

坐标类型的「核心流程」就是它的**运算语义**。以 `Point` 为例，加法规则是理解一切的关键（摘自 point.rs:74-84）：

- 右操作数 `other.row == 0`（同一行上的位移）→ 行不变、**列相加**：\((r, c) + (0, dc) = (r, c + dc)\)。
- `other.row > 0`（跨行位移）→ 行相加、**列被替换**：\((r, c) + (dr, c') = (r + dr, c')\)。因为跨过换行后，原列信息不再有意义，终点列只由位移自身决定。

减法是对称的（同行列相减；跨行则行相减、列保留被减数自己的列，point.rs:94-106，并 `debug_assert!` 不允许减出负数）。

比较语义是**字典序**：先比行、再比列。64 位平台上 `Ord` 用了一个打包技巧（point.rs:131-146）：

\[ \text{key}(p) = (p_{row} \ll 32) \,|\, p_{column} \]

把两个 u32 打包进一个 usize 后做一次整数比较，比两次分支比较更快；32 位平台则回退为普通的逐字段比较（`#[cfg(target_pointer_width = ...)]` 分别实现）。

`PointUtf16` 的运算与 `Point` **逐行同构**（把两份源码并排读，除了类型名几乎一样）；`OffsetUtf16` 是一维的，运算就是普通整数加减。

#### 4.3.3 源码精读

[crates/rope/src/point.rs:7-12](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L7-L12) —— `Point { pub row: u32, pub column: u32 }`：零索引行列，字段公开。文档注释只有一句，但口径重要：column 以 UTF-8 字节计。

[crates/rope/src/point.rs:14-18](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L14-L18) —— `Point` **手写**了 `Debug`，输出形如 `Point(2:5)`（冒号分隔，贴合「行列」直觉）。对比之下 `PointUtf16` 用的是 `#[derive(Debug)]`（输出 `PointUtf16 { row: 2, column: 5 }`）。这个差异在打印日志时一眼就能分辨两类坐标，是个实用细节。

[crates/rope/src/point.rs:44-51](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L44-L51) —— `Point::parse_str`：把一段文本的最后位置解析为 Point（逐行枚举，最后停留的 `row`/`column = line.len()`），测试里常用来构造期望值。

[crates/rope/src/point_utf16.rs:6-10](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point_utf16.rs#L6-L10) —— `PointUtf16 { pub row: u32, pub column: u32 }`：字段与 `Point` 完全同名同型，但 column 的**语义**是 UTF-16 code unit 数。Rust 类型系统无法表达「单位不同」，所以靠命名区分——这也提醒你：两类值混用前必须经过 rope 的换算 API（u2-l8）。

[crates/rope/src/point_utf16.rs:47-57](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point_utf16.rs#L47-L57) —— `PointUtf16` 的 `Add`：与 point.rs:74-84 逐行同构，验证「跨行列替换」规则在两套坐标系中一致成立。

[crates/rope/src/offset_utf16.rs:3-4](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/offset_utf16.rs#L3-L4) —— `pub struct OffsetUtf16(pub usize);`：最简单的一个，newtype 包装。它的存在不是包装数据，而是**包装单位**——让「UTF-16 偏移」和「字节偏移」在类型上不混。派生了一整套 `Ord`/算术运算（L6-49）。

#### 4.3.4 代码实践：三兄弟的 Debug 面孔

1. **实践目标**：用三行打印确认三个坐标类型的形态与 Debug 格式差异。
2. **操作步骤**：在第 5 节综合实践的外部小程序中加入（示例代码）：

   ```rust
   let p = Point::new(2, 5);
   let p16 = PointUtf16::new(2, 5);
   let o16 = OffsetUtf16(5);
   println!("{:?} / {:?} / {:?}", p, p16, o16);
   println!("{:?}", Point::parse_str("ab\ncdef"));
   ```
3. **需要观察的现象**：三种 Debug 输出的排版差异。
4. **预期结果**：第一行输出 `Point(2:5) / PointUtf16 { row: 2, column: 5 } / OffsetUtf16(5)`；第二行输出 `Point(1:4)`（两行文本，末行 4 字节）。若你的输出不同，请以本地实际运行为准。

#### 4.3.5 小练习与答案

**练习 1**：计算 `Point::new(0, 3) + Point::new(2, 5)` 和 `Point::new(2, 5) + Point::new(0, 3)`。

答案：前者 `(0,3)` 加上跨行位移 `(2,5)` → 行相加列替换 → `(2, 5)`；后者同行位移 → `(2, 8)`。两个结果不同，说明 `Point` 的加法**不满足交换律**——它是「位置 + 位移」的语义，不是向量加法。

**练习 2**：`Point` 和 `PointUtf16` 字段完全相同，为什么不合并成一个类型加个标志位？

答案：合并后两套坐标可以在类型层面自由混用，一旦把 UTF-16 列当字节列用（或反之），换算会悄悄错位——对一个 emoji（4 字节 = 2 个 UTF-16 单元）误差可达两倍。分成两个类型后，混用必须显式经过换算函数，把单位错误挡在编译期。

**练习 3**：`OffsetUtf16` 的 `Sub` 里为什么有 `debug_assert!(*other <= self)` 而不是直接返回 0？

答案：对一维偏移做减法，结果为负几乎必然意味着上游逻辑错误（坐标倒挂）。debug 构建下立即暴露；release 构建下 `usize` 减法溢出会 wrap 或 panic（取决于溢出检查配置），错误不会被静默吞掉。「宁可崩溃也不给出貌似合理的错误值」是数值类型的常见取舍。

### 4.4 unclipped.rs：Unclipped<T> 包装器

#### 4.4.1 概念说明

外部世界（LSP 服务器、协作对端）送来的坐标经常是「不合法」的：列超出行长、或落在多字节字符中间。rope 的 API 如果直接收 `PointUtf16`，调用者很容易把一个未校验的坐标传给只接受合法坐标的函数。于是 rope 用 `Unclipped<T>` 这个泛型 newtype 给「未裁剪坐标」发一张**临时身份证**：包着它进来，函数内部负责裁剪（clip）到最近合法边界，返回的则是干净的 `T`。这是「make invalid states unrepresentable」思想的低成本实现。

#### 4.4.2 核心流程

`Unclipped<T>` 的使用闭环（以 rope 的公开 API 为例）：

1. 外部拿到一个可能越界的 `PointUtf16`；
2. 包装成 `Unclipped<PointUtf16>`（`Unclipped(p)` 或 `Unclipped::from(p)`）；
3. 调用 `Rope::clip_point_utf16(point: Unclipped<PointUtf16>, bias)`（rope.rs:563）等以 `Unclipped` 为参数的 API；
4. 函数内部借用 chunk.rs:624 的 `ChunkSlice::clip_point_utf16` 完成实际裁剪；
5. 返回合法的 `PointUtf16`，`Unclipped` 随之消亡——它只存在于「入口」这一小段路上。

#### 4.4.3 源码精读

[crates/rope/src/unclipped.rs:1-11](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/unclipped.rs#L1-L11) —— 文件全貌的前三分之一：`pub struct Unclipped<T>(pub T);`（元组结构体，内部字段公开）和 `From<T>` 转换。注意第 1 行它 `use crate::ChunkSummary`——这个看似「纯数学」的小文件其实也挂在了 crate 根定义的摘要类型上。

[crates/rope/src/unclipped.rs:13-23](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/unclipped.rs#L13-L23) —— 关键实现：为 `Unclipped<T>` 实现了 `sum_tree::Dimension<'a, ChunkSummary>`，做法是**纯委托**——把 `zero`/`add_summary` 原样转发给内层 `T`。也就是说：包了 `Unclipped` 不改变累加行为，只是换了个类型标签，树依然可以按它做前缀和查找。

[crates/rope/src/unclipped.rs:25-51](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/unclipped.rs#L25-L51) —— `Add`/`Sub`/`AddAssign`/`SubAssign` 的泛型转发：只要内层 `T` 支持该运算，包装层就支持，且逐字段作用在内层值上。

#### 4.4.4 代码实践：包装与解包

1. **实践目标**：体会 `Unclipped` 只是个透明标签。
2. **操作步骤**：在外部小程序中加入（示例代码）：

   ```rust
   let dirty = Unclipped::from(PointUtf16::new(0, 999)); // 列远超行长
   let clean: PointUtf16 = rope.clip_point_utf16(dirty, Bias::Right);
   println!("{:?}", clean);
   ```

   其中 `rope` 是 `Rope::from("hi")`，`Bias` 需要 `use sum_tree::Bias;`（你的外部 crate 需额外依赖 `sum_tree`，路径同 rope）。
3. **需要观察的现象**：越界列被裁到行内最后一个合法边界。
4. **预期结果**：对两字符的 ASCII 文本，输出 `PointUtf16 { row: 0, column: 2 }`（裁到行尾）。含 emoji 时 Right 偏置会落在完整字素之后——具体值待本地验证（u2-l8 会系统讲解 clip 语义）。

#### 4.4.5 小练习与答案

**练习 1**：`Unclipped<PointUtf16>` 和 `PointUtf16` 在内存布局上有什么区别？

答案：几乎没有区别——newtype 是零开销抽象，大小、对齐都和内层一致。它付出的唯一「成本」是要求调用者多写一层包装，换取编译期的类型隔离。

**练习 2**：既然 `Unclipped` 的 `Dimension` 实现是纯委托，为什么还要写这个 impl？

答案：因为 `Dimension` 是 trait，`Unclipped<T>` 是**新的类型**，不实现 trait 就不能作为维度参与 `sum_tree` 的查找。委托实现让「带标签的坐标」可以和原坐标走完全相同的树查询路径，裁剪逻辑无需另写一套查找。

**练习 3**：找出 rope 公开 API 中两个以 `Unclipped` 为参数的方法。

答案：`Rope::unclipped_point_utf16_to_offset`（rope.rs:487）、`Rope::unclipped_point_utf16_to_point`（rope.rs:522）、`Rope::clip_point_utf16`（rope.rs:563）——任答两个即可。共同点：入参可能不合法，出参保证合法。

### 4.5 全景：模块依赖图与导出面清单

#### 4.5.1 概念说明

把 4.1–4.4 的观察拼成一张图，就得到 crate 的完整模块依赖图。它能回答三个问题：改哪个文件会影响谁、新类型应该放哪个文件、以及「叶子—躯干—门面」的分层为什么稳定。

#### 4.5.2 核心流程

当前 HEAD 的模块依赖关系（箭头表示「use 了对方的东西」）：

```text
        ┌─────────────── 只依赖 std 的叶子层 ───────────────┐
        │   point.rs (Point)   point_utf16.rs (PointUtf16)   offset_utf16.rs (OffsetUtf16)
        └────────┬──────────────────────┬──────────────────────────┘
                 │ use crate::{OffsetUtf16, Point, PointUtf16,        │
                 │             TextSummary, Unclipped}  (chunk.rs:1)  │ use crate::ChunkSummary (unclipped.rs:1)
                 ▼                                              ▼
             chunk.rs (Chunk / ChunkSlice / Bitmap / 位图运算)   unclipped.rs (Unclipped<T>)
                 ▲
                 │ use chunk::{Bitmap, MIN_BASE, MAX_BASE} (rope.rs:23 等)
                 │
        rope.rs —— crate 根（[lib] path 指向它）
        mod 声明 5 个子模块 + pub use 再导出 7 个类型
        居住着 Rope / Cursor / Chunks / Bytes / Lines /
        TextSummary / ChunkSummary / TextDimension / DimensionPair
```

两个值得指出的结构事实：

1. **根与 chunk 互相引用**（rope.rs `use chunk::...`，chunk.rs `use crate::TextSummary`）。同一 crate 内的模块循环完全合法；真正被禁止的是 crate 之间的循环依赖。
2. **坐标三兄弟谁也不依赖谁**，与 `chunk.rs`、`rope.rs` 之间只靠 crate 根的再导出间接相连——这意味着你可以单独把 point.rs 拷出去理解，不丢任何上下文。

#### 4.5.3 源码精读

对外导出面 = `pub use` 的 7 项 + rope.rs 直接 `pub` 的 11 项，汇总如下（与 4.1.3 表格互为索引）：

| 来源 | 类型 | 引入方式 |
| --- | --- | --- |
| chunk.rs | `Chunk`、`ChunkSlice` | [rope.rs:17](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L17) 再导出 |
| offset_utf16.rs | `OffsetUtf16` | [rope.rs:18](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L18) 再导出 |
| point.rs | `Point` | [rope.rs:19](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L19) 再导出 |
| point_utf16.rs | `PointUtf16` | [rope.rs:20](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L20) 再导出 |
| unclipped.rs | `Unclipped` | [rope.rs:21](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L21) 再导出 |
| rope.rs | `Rope`、`Cursor`、`ChunkBitmaps`、`Chunks`、`ChunkWithBitmaps`、`Bytes`、`Lines`、`ChunkSummary`、`TextSummary`、`TextDimension`（trait）、`DimensionPair` | 直接 `pub` 定义 |

刻意**不导出**的：`Bitmap`、`MIN_BASE`、`MAX_BASE`（均 `pub(crate)`，chunk.rs:8-14）、`Tabs`/`TabPosition`（chunk.rs:685-694，无 `pub` 前缀故仅在 crate 内可见）、`Chunk::MASK_BITS` 等。它们是实现细节，改动不必经过语义化版本的门面。

#### 4.5.4 代码实践：手绘依赖图并对照源码自查

1. **实践目标**：不看讲义，凭源码重画 4.5.2 的依赖图。
2. **操作步骤**：
   1. 打开每个文件的 import 区（各文件前 6 行）。
   2. 在纸上为每个文件画一个框，凡出现 `use crate::X` 就从该文件画箭头到 X 的定义地；凡出现 `mod X;` 画一条虚线表示「声明」。
   3. 对照本讲 4.5.2 的图检查箭头数量与方向。
3. **需要观察的现象**：point.rs / point_utf16.rs / offset_utf16.rs 的 import 区只有 `std::`；chunk.rs 和 unclipped.rs 各有一条指回 crate 根的箭头。
4. **预期结果**：图与 4.5.2 完全一致（共 2 条 crate 内 use 箭头指向 chunk.rs 与 unclipped.rs、1 条从根指向 chunk.rs 的反向箭头；叶子层无入边箭头）。

#### 4.5.5 小练习与答案

**练习 1**：如果要在 rope 里新增一个类型 `ColumnUtf32`（UTF-32 列坐标），按现有分层应该放进哪个文件、要不要改 rope.rs？

答案：放进一个新文件（或与现有坐标并列的文件），只依赖 std；然后在 rope.rs 加 `mod column_utf32;` 与 `pub use column_utf32::ColumnUtf32;`，并在 rope.rs 里为它补 `Dimension`/`TextDimension` 实现。分层规则：坐标语义进叶子层，与树的集成进根。

**练习 2**：`Tabs` 迭代器（chunk.rs:685）为什么不像 `Chunk` 一样再导出？

答案：`Tabs` 服务于编辑器层的 tab 布局，是 rope 的「顺带产出」而非核心文本坐标；不导出可以让它自由演进（改字段、改签名）而不构成对外承诺。导出面越大，未来重构的束缚越多。

**练习 3**：判断真假：「`ChunkSummary` 是 `TextSummary` 的再导出别名」。为什么？

答案：假。`ChunkSummary` 是独立的 struct（rope.rs:1266），内部**包含**一个 `TextSummary` 字段，并实现了 `sum_tree::ContextLessSummary`（rope.rs:1270-1278）。包含不是别名：它存在的原因是 `sum_tree` 要求树上挂的 Summary 是一个具体类型，`Chunk` 的 `Item::Summary` 关联类型指向它（rope.rs:1255-1262）。

## 5. 综合实践

把本讲所有观察合并成一个可运行的外部小程序：**「rope 导出面巡检器」**。它一次性验证规格中的实践任务——画依赖图 + 引用每个公开类型 + 写一句话职责注释。

**第 1 步：画依赖图。** 先完成 4.5.4 的手绘图（或用任何画图工具/mermaid），这一步不需要写代码。

**第 2 步：创建外部 crate。** 在 zed 仓库**外面**的任意目录（下例用 `~/playground`，路径按你的实际情况调整）：

```bash
cargo new rope_api_tour
cd rope_api_tour
```

编辑 `Cargo.toml`（示例代码）：

```toml
[package]
name = "rope_api_tour"
version = "0.1.0"
edition = "2021"

[dependencies]
rope = { path = "/path/to/zed/crates/rope" }
sum_tree = { path = "/path/to/zed/crates/sum_tree" } # 只为引入 Bias 枚举
```

**第 3 步：编写巡检程序。** 把 `src/main.rs` 替换为（示例代码，注释即「一句话职责」作业）：

```rust
use rope::{
    Chunk, ChunkSlice, OffsetUtf16, Point, PointUtf16, Rope, TextSummary, Unclipped,
};
use sum_tree::Bias;

fn main() {
    // Rope： rope.rs —— 文本本体，前缀和树 SumTree<Chunk> 的门面类型
    let rope = Rope::from("ab\ncd中");

    // Point： point.rs —— UTF-8 字节口径的行列坐标
    let p = Point::new(1, 2) + Point::new(0, 3);
    println!("Point 加法: {:?}", p);

    // PointUtf16： point_utf16.rs —— UTF-16 口径的行列坐标
    let p16 = PointUtf16::new(0, 999);
    println!("未裁剪: {:?}", p16);

    // Unclipped： unclipped.rs —— 「未裁剪坐标」的类型标签
    let clipped = rope.clip_point_utf16(Unclipped(p16), Bias::Right);
    println!("裁剪后: {:?}", clipped);

    // OffsetUtf16： offset_utf16.rs —— UTF-16 口径的一维偏移
    // "ab\n" 共 3 个 UTF-16 单元，所以字节偏移 3 对应 UTF-16 偏移 3
    let o16 = rope.offset_to_offset_utf16(3);
    println!("字节偏移 3 的 UTF-16 偏移: {:?}", o16);

    // Chunk： chunk.rs —— 定长文本块（≤128 字节）+ 四张位图
    let chunk = Chunk::new("hi\n");
    println!(
        "chunk: {} 字节, {} 个字符起始位, {} 个换行位",
        chunk.text.len(),
        chunk.chars().count_ones(),
        chunk.newlines().count_ones(),
    );

    // ChunkSlice： chunk.rs —— 块的零拷贝切片视图
    let mid = chunk.text.len() / 2;
    let (left, _right): (ChunkSlice, ChunkSlice) = (chunk.slice(0..mid), chunk.slice(mid..));
    println!("左半块是否为空: {}", left.is_empty());

    // TextSummary： rope.rs —— 一段文本的完整统计快照
    let summary: TextSummary = rope.summary();
    println!("summary: {:?}", summary);

    println!("Rope Debug: {:?}", rope);
}
```

**第 4 步：运行与观察。**

```bash
cargo run
```

- **预期输出**（关键行）：`Point 加法: Point(1:5)`；`未裁剪: PointUtf16 { row: 0, column: 999 }`；`字节偏移 3 的 UTF-16 偏移: OffsetUtf16(3)`；`chunk: 3 字节, 3 个字符起始位, 1 个换行位`；`summary` 一行以 `len: 8` 开头（`"ab\ncd中"` = 2+1+2+3 = 8 字节、6 个字符，第二行 5 字节）。
- `cargo run` 首次编译会连带构建 rope 的依赖链（`sum_tree`、`util`、`ztracing` 等），需要几分钟。
- 若你的输出与预期有出入（尤其裁剪和 summary 的细节），以本地实际输出为准，并回到对应源码行核对——这正是本练习的目的。
- 本讲作者未在此环境实际运行该程序，以上为依据源码推演的预期结果，**待本地验证**。

**第 5 步：扩展巡检（可选）。** 在 `use` 列表中继续加入 `Cursor`、`Chunks`、`Bytes`、`Lines`、`ChunkSummary`、`TextDimension`、`DimensionPair`，并各配一句话注释。若某个名字编译报错，说明它不在导出面上——回头检查 4.5.3 的清单，思考为什么。

## 6. 本讲小结

- rope 的 6 个源文件呈「叶子—躯干—门面」三层：3 个只依赖 std 的坐标叶子（point.rs、point_utf16.rs、offset_utf16.rs）、1 个躯干（chunk.rs 的块与位图）、1 个门面 crate 根（rope.rs，兼当 `lib.rs`），外加标签文件 unclipped.rs。
- crate 根用私有 `mod` + `pub use` 收窄 API 面：外部只能写 `rope::Chunk` 而不能写 `rope::chunk::Chunk`；`Bitmap`、`MIN_BASE`、`MAX_BASE` 等实现细节用 `pub(crate)` 留在内部。
- `rope.rs` 独占全 crate 三分之二以上体量：`Rope`、`Cursor`、`Chunks`/`Bytes`/`Lines`、`TextSummary`/`ChunkSummary`、`TextDimension` 都住在这里，文件尾部还有 ~800 行集中测试（含 test-only 的 `Rope::text`）。
- `Point` 的加法是「位置 + 位移」语义且不满足交换律；`Point` 与 `PointUtf16` 字段相同但**单位不同**，靠类型隔离防止混用。
- `Unclipped<T>` 是零成本的「未裁剪坐标」标签，其 `Dimension` 实现纯委托，专用于 clip 系列入口 API。
- crate 内模块允许互相引用（rope.rs ↔ chunk.rs），这与 crate 间禁止循环依赖并不冲突。

## 7. 下一步学习建议

下一讲 **u1-l3《Rope 上手：构建、读取与修改文本》** 将在本地图的基础上实操 `Rope` 的日常 API：`push`/`append`/`slice`/`replace`、`len`/`summary`、`chars`/`chunks` 等。建议你在进入下一讲前：

- 把本讲的依赖图默画一遍，做到六个文件脱口而出职责。
- 通读 [crates/rope/src/rope.rs:1-23](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1-L23) 直到能解释每一行的作用（`mod` / `use` / `pub use` 三类声明的差别）。
- 有余力可提前浏览 [crates/rope/src/point.rs:44-51](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L44-L51)（`parse_str`）和 rope.rs 测试模块里对它的使用，为 u2-l1 的坐标系统深挖做铺垫。
