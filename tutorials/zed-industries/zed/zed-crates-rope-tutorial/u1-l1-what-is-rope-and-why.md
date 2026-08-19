# rope 是什么：Zed 文本引擎的地基

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `rope` crate 在 Zed 中的角色：它是编辑器文本缓冲区（buffer）的底层数据结构，被 `text`、`editor`、`multi_buffer` 等十多个上层 crate 依赖。
2. 理解 Rope 相比 `String` 在频繁编辑场景下的优势：为什么编辑器不能每次按键都拷贝整份文本。
3. 读懂 `Rope` 结构体的定义、它的几个 O(1) 查询方法，以及 `Cargo.toml` 中每个依赖的用途。
4. 成功构建并运行 rope 的测试，并写出一个使用 `Rope::from` 的最小示例程序。

本讲是整套手册的第一篇，不假设你读过任何 Zed 源码。

## 2. 前置知识

### 2.1 什么是文本缓冲区

编辑器里你正在编辑的那份文本，在内存中被称为**缓冲区**（buffer）。你在 Zed 中打开一个文件，磁盘上的文件并不会一边编辑一边写回，而是先完整地装进一个内存数据结构——这个结构就是缓冲区，而 `rope` crate 正是给这个结构提供底层存储的。

### 2.2 为什么 `String` 不够用

Rust 的 `String` 本质是 `Vec<u8>`：一段**连续**内存。连续带来读取上的好处，但也带来写入上的代价：

- 在第 1 个字符后插入 1 个字符，需要把后面所有字节整体向后搬移，代价是 \( O(n) \)。
- 删除一段文本同样要向前搬移填补空隙。
- 打开一个 100 MB 的文件，如果用 `String` 存储，那么**每一次按键**都可能伴随上百兆字节的内存拷贝。

对普通程序这无所谓，但对「每秒可能处理几十次编辑、还要同时支持语法高亮、diff、协作同步」的编辑器来说，这是不可接受的。

### 2.3 rope（绳子）的直觉

想象一根很长的绳子，你要在中间接一段新绳子进去：你不需要把整根绳子拆散重编，只需要在接点处打个结。rope 数据结构就是这个思路：

- 把长文本切成一个个**小块**（chunk），每个块只有几十到一百多个字节。
- 用一棵**平衡树**把这些块串起来，逻辑顺序就是树的中序遍历顺序。
- 在中间插入文本，只影响一个块（可能分裂成两个）加上从树根到该块的 \( O(\log n) \) 条路径。
- 树的每个节点额外缓存子树的**摘要**（summary：总字节数、总行数等），于是「这份文本有多少字节」「最后一行在哪」这类查询不需要遍历，直接读树根就是 \( O(1) \)。

Zed 的实现里，「平衡树 + 摘要」由自研的 `sum_tree` crate 提供，「小块」由 `Chunk` 类型提供（定长缓冲 + 四张位图，后续讲义会深入）。

### 2.4 你需要的一点 Rust 与 Cargo 常识

- `cargo test -p <包名>`：只运行某个包的测试。
- Cargo 的 **workspace 机制**：一个仓库里多个 crate 共享依赖版本声明。成员 crate 的 `Cargo.toml` 里写 `xxx.workspace = true`，表示「版本和 features 以根 `Cargo.toml` 的 `[workspace.dependencies]` 为准」。
- 阅读本讲不需要了解 GPUI 或 Zed 其他子系统。

## 3. 本讲源码地图

本讲只涉及两个文件（后续讲义会逐步展开其余文件）：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/rope/Cargo.toml` | crate 清单：库根路径、依赖、基准配置 | 依赖清单各是干什么的 |
| `crates/rope/src/rope.rs` | crate 根与核心类型：`Rope`、`TextSummary`、游标与迭代器 | `Rope` 结构体定义与 O(1) 查询 |
| 仓库根 `Cargo.toml` | workspace 依赖版本总表 | `.workspace = true` 的解析来源 |

`src/rope.rs` 的开头集中声明了其余子模块并对外导出（chunk、point 等将在 u1-l2 逐个介绍）：

[crates/rope/src/rope.rs#L1-L23](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1-L23) —— 声明 5 个子模块（`chunk`、`offset_utf16`、`point`、`point_utf16`、`unclipped`），并 `pub use` 导出 `Chunk`、`ChunkSlice`、`OffsetUtf16`、`Point`、`PointUtf16`、`Unclipped` 六个公开类型。这些就是 crate 的全部对外 API 面。

另外先给出一张「生态位置图」：在仓库根目录用 `rg -l 'rope.workspace' --glob '**/Cargo.toml'` 可以找到所有依赖 rope 的 crate，包括 `text`、`editor`、`multi_buffer`、`git`、`streaming_diff`、`buffer_diff`、`fs`、`languages`、`outline`、`picker_preview`、`go_to_line`、`agent_ui`、`zed`。粗略的分层是：

```
zed / agent_ui / outline ...        ← 应用与功能层
        │
editor / multi_buffer               ← 编辑器视图与多缓冲区
        │
text (Buffer 就在这里)               ← 文本缓冲区层
        │
rope  ←★★★ 你在这里：最底层的文本存储
        │
sum_tree / heapless / rayon ...     ← 通用基础设施
```

## 4. 核心概念与源码讲解

### 4.1 Rope 结构体：一颗装满文本块的树

#### 4.1.1 概念说明

`Rope` 是这个 crate 的主角，但它的定义简短得令人意外——整个结构体只有一个字段：

```rust
#[derive(Clone, Default)]
pub struct Rope {
    chunks: SumTree<Chunk>,
}
```

含义拆解：

- `Chunk`：一小段文本（上限 128 字节的定长缓冲），携带四张位图记录「哪些位置是字符边界 / 换行 / 制表符」。
- `SumTree<Chunk>`：Zed 自研前缀和树（来自 `crates/sum_tree`），节点按顺序挂着这些 chunk，并在每个节点缓存子树摘要。

所以**Rope = 有序的 chunk 集合 + 每个节点上的缓存统计**。它解决的核心问题是：

1. **编辑局部化**：改一个字只触碰一个 chunk，不搬移整份文本。
2. **查询加速**：字节数、行数、最长行这类统计被「缓存」在树里，读树根即可。
3. **跨 crate 的公共坐标语言**：所有对文本的定位（字节偏移、行列、UTF-16 偏移）都以 Rope 上的方法提供，上层（如 LSP 协议对接、编辑器光标）都建立在其上。

`Clone` 让绳子可以廉价地被快照（配合 `SumTree` 的结构共享），这在 Zed 的撤销/协作里非常关键；`Default` 给出空绳子。

#### 4.1.2 核心流程

**构建流程**（从 `&str` 得到 Rope）：

```text
Rope::from("hello\nworld")
  └─ Rope::new()            # 空 SumTree
  └─ rope.push(text)
       ├─ 尝试把 text 塞进现有的最后一块（能塞下就塞，塞不下先切一刀）
       ├─ 剩余文本按 MAX_BASE(128B) 逐段切开，
       │    切点若落在多字节字符中间则回退到字符边界
       └─ 每一段构造一个 Chunk，追加进 SumTree
```

**查询流程**（以 `len()` 为例）：

```text
rope.len()
  └─ self.chunks.extent(())   # 读树根缓存的总字节数，O(1)
```

`summary()`、`max_point()` 同理：它们不遍历文本，而是读取树的聚合信息。这是 rope 相对 `String` 的第二个优势——`String` 统计行数要 \( O(n) \) 扫描，Rope 是 \( O(1) \)。

**复杂度对比速查**：

| 操作 | `String` | `Rope` |
| --- | --- | --- |
| 末尾追加 | 均摊 \( O(1) \) | \( O(\log n) \)（含分块） |
| 中间插入/删除 | \( O(n) \) 搬移 | \( O(\log n) \) 定位 + 小块内常数操作 |
| 取总字节数 | \( O(1) \) | \( O(1) \)（树根摘要） |
| 统计行数/最长行 | \( O(n) \) | \( O(1) \)（树根摘要） |
| 连接两段文本 | \( O(n) \) 拷贝 | \( O(\log n) \) |
| 按字节偏移随机访问 | \( O(1) \) | \( O(\log n) \)（先定位 chunk） |

可以看到 Rope 用「随机访问变慢」换来了「编辑、拼接、统计全面变快」——这正是编辑器的工作负载特征。

#### 4.1.3 源码精读

**① 结构体定义与构造**

[crates/rope/src/rope.rs#L25-L33](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L25-L33) —— `Rope` 只有一个字段 `chunks: SumTree<Chunk>`；`new()` 直接走 `Default`，返回一棵空树。定义极简是因为所有复杂度都被推给了 `SumTree` 和 `Chunk` 两个协作方。

**② O(1) 统计查询**

[crates/rope/src/rope.rs#L312-L330](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L312-L330) —— 这五个方法是本讲最推荐记住的 API：

- `summary()` 返回 `TextSummary`（整份文本的统计快照）；
- `len()` 返回总字节数（注意是 **UTF-8 字节数**，不是字符数）；
- `is_empty()`；
- `max_point()` 返回文本末尾的行列坐标 `Point`；
- `max_point_utf16()` 返回 UTF-16 版本的末尾坐标。

它们的实现都只有一行，分别委托给 `self.chunks.summary()` / `self.chunks.extent(())`——「查询即读缓存」的直接证据。

**③ 从字符串构建**

[crates/rope/src/rope.rs#L621-L651](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L621-L651) —— 四个 `From` 转换：`From<&str>` 走 `new()` + `push(text)`；`FromIterator<&str>` 支持从迭代器收集（比如把网络分块流收集成一根绳子）；`From<String>` 与 `From<&String>` 借用 `&str` 版本。这就是写 `Rope::from("hello\nworld")` 时真正执行的代码。

**④ push 的第一步：先填满最后一块**

[crates/rope/src/rope.rs#L147-L168](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L147-L168) —— `push` 先用 `update_last` 尝试填充现有最后一块：如果装得下（不超过 `MAX_BASE`）就整段塞入；装不下则计算一个切分点（至少把最后一块补到 `MIN_BASE`），且切分点必须落在 UTF-8 字符边界上（`while !text.is_char_boundary(split_ix)`）。剩余文本再走后续分块循环。完整分块策略留到 u2-l6 精读，这里只需建立「push = 填尾块 + 切新块」的印象。

**⑤ 打印整根绳子**

[crates/rope/src/rope.rs#L653-L660](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L653-L660) —— `Display` 实现就是逐个 chunk 写出去，没有一次性拼大 `String`。所以 `rope.to_string()` 是 \( O(n) \) 且会产生一份完整拷贝——调试时方便，但性能敏感路径上应该用 `chunks()` 迭代器直接消费 `&str`。

**⑥ TextSummary：统计快照长什么样**

[crates/rope/src/rope.rs#L1280-L1304](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1280-L1304) —— `TextSummary` 的九个字段：`len`（字节）、`chars`（字符数）、`len_utf16`（UTF-16 码元数）、`lines`（末尾 `Point`）、`first_line_chars` / `last_line_chars` / `last_line_len_utf16`、`longest_row` / `longest_row_chars`。本讲只会用到 `len` 和 `chars`，其余字段在 u2-l2 专门展开。

顺带看一眼统计是怎么算出来的：

[crates/rope/src/rope.rs#L1337-L1383](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1337-L1383) —— `From<&str> for TextSummary` 逐字符扫描：遇 `\n` 就 `lines += Point::new(1, 0)`（行号加一、列清零），否则 `lines.column += c.len_utf8()`（**列以 UTF-8 字节计**）。两个细节值得现在就记住：一是 `Point` 的 column 是字节列；二是 `longest_row` 只在严格大于时更新（`last_line_chars > longest_row_chars`），并列时保留先出现的一行。

#### 4.1.4 代码实践

**实践目标**：亲手用 `Rope::from` 构建绳子，验证 O(1) 统计查询的返回值，并确认本地环境能编译运行本 crate。

**操作步骤**：

1. 先在仓库根目录运行测试，确认环境可用：

   ```bash
   cd <zed 仓库根目录>
   cargo test -p rope
   ```

2. 创建示例程序。**注意**：`rope` 是 workspace 成员，它的 `Cargo.toml` 使用 `edition.workspace = true` 这类 workspace 继承写法，所以在 zed 仓库**外面**用 `cargo new` 新建项目再以 path 依赖引入是行不通的（Cargo 会报缺少 workspace 上下文）。推荐做法是使用 cargo 自动发现的 examples 目录：

   ```bash
   mkdir -p crates/rope/examples
   ```

   新建 `crates/rope/examples/hello_rope.rs`（示例代码，非项目原有文件）：

   ```rust
   use rope::Rope;

   fn main() {
       let rope = Rope::from("hello\nworld");

       // len() 是 UTF-8 字节数，不是字符数
       println!("字节数: {}", rope.len());

       // 字符数藏在 TextSummary 里
       println!("字符数: {}", rope.summary().chars);

       // 末尾坐标：(row, column)，column 按字节计
       println!("最大点: {:?}", rope.max_point());

       // 顺便看看整份统计
       println!("摘要: {:?}", rope.summary());

       // 逐块消费：不产生整份拷贝
       for chunk in rope.chunks() {
           println!("一块: {:?}", chunk);
       }
   }
   ```

3. 运行：

   ```bash
   cargo run -p rope --example hello_rope
   ```

**需要观察的现象**：

- 测试命令最后输出 `test result: ok.`，且包含大量 `rope::tests::...` 条目（这个 crate 的测试非常多）。
- 示例程序打印出统计值。

**预期结果**（依据源码推断，待本地验证）：

```text
字节数: 11
字符数: 11
最大点: Point(1:5)
摘要: TextSummary { len: 11, chars: 11, ... }
一块: "hello\nworld"
```

推断依据：`"hello\nworld"` 共 11 个 ASCII 字节；换行使 `lines` 变为 `Point(1:5)`（第 1 行第 5 字节处）；`max_point()` 直接返回这个值。注意 `Point` 的 `Debug` 输出格式是 `Point(行:列)`（见 [crates/rope/src/point.rs#L14-L18](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/point.rs#L14-L18)）。短文本只占一个 chunk，所以只打印一块；`TextSummary` 的完整 `Debug` 字段较多，此处用 `...` 略写，实际输出请以本地为准。

#### 4.1.5 小练习与答案

**练习 1**：把示例文本换成 `"中文ab"`（共 2 个汉字 + 2 个字母，UTF-8 下占 8 字节），`rope.len()`、`rope.summary().chars`、`rope.max_point()` 分别是多少？

**答案**：`len()` = 8（字节数：每个汉字 3 字节）；`chars` = 4（字符数）；`max_point()` = `Point(0:8)`——没有换行符，行号为 0，而 column 按**字节**累计（`lines.column += c.len_utf8()`），所以是 8 不是 4。这道题提醒你：`len` 与 `max_point` 的 column 都是字节口径，字符口径要查 `summary().chars`。

**练习 2**：`rope.to_string()` 和逐块 `chunks()` 迭代，哪个会产生完整文本拷贝？各适合什么场景？

**答案**：`to_string()` 会（走 `Display`，逐块 `write!` 进同一个 `String`）；`chunks()` 不会，它每次只借用一小段 `&str`。调试打印、传给需要完整字符串的旧接口时用 `to_string()`；流式处理（写文件、网络发送、语法分析）时用 `chunks()`。

**练习 3**：`Rope` 的 `#[derive(Clone)]` 意味着什么？为什么 Zed 的撤销（undo）和协作功能会喜欢这个性质？

**答案**：克隆一根 Rope 不需要拷贝所有文本——`SumTree` 内部是共享不可变结构（结构共享），克隆只是复制树根句柄。于是「拍快照」非常便宜，撤销栈可以保留编辑前的绳子视图，协作同步也能各持一份一致的历史状态而不用担心内存翻倍。

### 4.2 Cargo.toml 依赖清单：rope 的外部支撑

#### 4.2.1 概念说明

看懂一个 crate 的第一步往往是看它「靠什么活着」。`crates/rope/Cargo.toml` 一共声明了 8 个正式依赖、6 个开发依赖，每一个都有明确分工：

| 依赖 | 类型 | 用途 |
| --- | --- | --- |
| `sum_tree` | 正式 | Zed 自研的前缀和树，`Rope` 的骨架（`SumTree<Chunk>`） |
| `heapless` | 正式 | 提供定长容器（`ArrayString` / `Vec`），让 `Chunk` 把文本存进栈上定长缓冲 |
| `rayon` | 正式 | 数据并行框架；构建超大文本时并行分块（`push_large` 路径） |
| `unicode-segmentation` | 正式 | Unicode 字素簇（grapheme cluster）切分，用于把外部坐标裁剪到安全边界 |
| `log` | 正式 | 轻量日志门面；非法字节偏移等错误以日志形式上报 |
| `util` / `ztracing` / `tracing` | 正式 | Zed 内部通用工具、追踪插桩 |
| `gpui`（test-support）、`rand`、`criterion`、`ctor`、`zlog` | 开发 | 参数化测试、随机测试、基准测试等，只在测试/基准时编译 |

另外两个配置项值得注意：

- `[lib] path = "src/rope.rs"`：库根直接叫 `rope.rs` 而不是默认的 `lib.rs`。这是 Zed 仓库的编码规范（见仓库 CLAUDE.md：「prefer specifying the library root path in Cargo.toml」），让文件名与模块语义一致。
- `[[bench]] name = "rope_benchmark" harness = false`：声明 criterion 基准（关闭 libtest harness），对应 `benches/rope_benchmark.rs`，u3-l3 会精读。

#### 4.2.2 核心流程

cargo 解析这份清单的流程：

```text
读取 crates/rope/Cargo.toml
  └─ 遇到 heapless.workspace = true
       └─ 去仓库根 Cargo.toml 的 [workspace.dependencies] 查
            heapless = "0.9.2"          # 版本与 features 以此为准
  └─ 遇到 sum_tree.workspace = true
       └─ 查到 sum_tree = { path = "crates/sum_tree" }
            # workspace 内部的 path 依赖
  └─ 汇总依赖图，编译
```

好处是：几十个 crate 共用的依赖版本收口在一处，升级时不会出现「同一个 crate 两个版本」的漂移。

#### 4.2.3 源码精读

**① crate 清单与库根**

[crates/rope/Cargo.toml#L1-L12](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/Cargo.toml#L1-L12) —— 包名 `rope`、GPL-3.0-or-later 许可证、`[lib] path = "src/rope.rs"` 指定库根（代替默认 `lib.rs`）。`edition.workspace = true` 与 `publish.workspace = true` 都继承自 workspace。

**② 正式依赖**

[crates/rope/Cargo.toml#L14-L22](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/Cargo.toml#L14-L22) —— 全部 8 个正式依赖都以 `.workspace = true` 声明，对应根 `Cargo.toml` 里的版本定义，例如 [Cargo.toml#L465](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/Cargo.toml#L465)（`sum_tree = { path = "crates/sum_tree" }`，workspace 内 crate）、[Cargo.toml#L630](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/Cargo.toml#L630)（`heapless = "0.9.2"`）、[Cargo.toml#L765](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/Cargo.toml#L765)（`rayon = "1.8"`）、[Cargo.toml#L872](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/Cargo.toml#L872)（`unicode-segmentation = "1.10"`）。

**③ 开发依赖与基准声明**

[crates/rope/Cargo.toml#L24-L34](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/Cargo.toml#L24-L34) —— `[dev-dependencies]` 里的 `gpui`（带 `test-support` feature）、`rand`、`criterion` 等只在测试与基准编译时引入；`[[bench]]` 段把 `benches/rope_benchmark.rs` 注册为非 libtest 的 criterion 基准。**注意一个信号**：一个纯数据结构 crate 的测试竟然依赖 GPUI（Zed 的 UI 框架），这是因为测试大量使用其参数化测试工具——这本身就是 u3-l2 要研究的话题。

#### 4.2.4 代码实践

**实践目标**：验证依赖清单能被正确解析、测试套件通过，并亲眼看到依赖树。

**操作步骤**：

1. 在仓库根目录运行：

   ```bash
   cargo test -p rope
   ```

   观察输出中的编译过程与最终 `test result` 行。

2. 查看 rope 的依赖树（只读命令，不改动任何文件）：

   ```bash
   cargo tree -p rope --depth 1
   ```

3. （可选）查看基准列表而不运行：

   ```bash
   cargo bench -p rope -- --list
   ```

**需要观察的现象**：

- 步骤 1：先编译 `sum_tree`、`heapless` 等依赖，再编译 `rope` 本体，随后输出一长串 `test rope::tests::test_... ok`；最终有 `test result: ok.` 与通过数量（该 crate 测试数量很大，几百个，具体数字待本地验证）。
- 步骤 2：输出以 `rope v0.1.0` 为根，能看到 `sum_tree`、`heapless`、`rayon`、`unicode-segmentation` 等直接依赖。
- 步骤 3：列出若干基准条目名（如随机生成相关分组），不实际运行测量。

**预期结果**：测试全部通过；依赖树第一层与 4.2.1 表格的「正式依赖」一列吻合。若测试失败，优先检查 Rust 工具链版本是否符合仓库要求（见仓库 `rust-toolchain.toml`）。

#### 4.2.5 小练习与答案

**练习 1**：`rayon` 出现在正式依赖里，但日常插入几个字符根本用不到并行。猜猜它服务于哪条代码路径？

**答案**：`push_large`（大文本构建）路径。当一次 push 的文本超过阈值（`NUM_CHUNKS * MAX_BASE` 级别，见 [crates/rope/src/rope.rs#L175-L185](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L175-L185)）时，会用 rayon 并行地把文本切分构造 chunk（`IntoParallelIterator` / `ParallelIterator`，见文件头部 [crates/rope/src/rope.rs#L8](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L8)）——典型场景是打开一个大文件。细节在 u2-l6 展开。

**练习 2**：为什么 `criterion`、`rand` 放在 `[dev-dependencies]` 而不是 `[dependencies]`？如果把 `rand` 挪到正式依赖，会有什么后果？

**答案**：`rand` 只被测试用来生成随机文本、`criterion` 只被基准用到，它们不参与 crate 的运行时功能。放在 dev-dependencies 意味着下游用户编译/发布 rope 时完全不引入这两个依赖。若挪到正式依赖，所有依赖 rope 的 crate（editor、git、fs……）都会被迫多编译并链接一份随机数库，无谓地拖慢构建、扩大供应链面。

**练习 3**：`Cargo.toml` 里 `[package.metadata.cargo-machete] ignored = ["tracing"]`（[crates/rope/Cargo.toml#L36-L37](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/Cargo.toml#L36-L37)）大概是干什么的？

**答案**：`cargo-machete` 是检测「声明了却没用上」依赖的工具。`tracing` 在源码里主要通过 `ztracing` 间接使用/或仅以宏插桩形式出现，machete 的静态扫描会误报它未使用，所以显式加入忽略名单。这属于仓库工程卫生配置，与运行时行为无关。

## 5. 综合实践

把本讲两个模块串起来，做一个「rope 体检小工具」：

1. 按 4.1.4 的步骤确认 `cargo test -p rope` 通过。
2. 把 `crates/rope/examples/hello_rope.rs` 扩展成下面这样（示例代码），对同一段含中文与多行文本同时输出「字节口径」和「字符口径」的统计，并对比 `String` 的等价计算（示例代码）：

   ```rust
   use rope::Rope;

   fn main() {
       let text = "第一行abc\nsecond line\n尾行";
       let rope = Rope::from(text);
       let s = text.to_string(); // String 等价模型，用作对照

       // 1. 字节口径
       assert_eq!(rope.len(), s.len());
       // 2. 字符口径
       assert_eq!(rope.summary().chars, s.chars().count());
       // 3. 行数：summary().lines.row + 1（最后一行没有换行符也要算一行）
       let line_count_rope = rope.summary().lines.row as usize + 1;
       let line_count_string = s.lines().count();
       assert_eq!(line_count_rope, line_count_string);
       // 4. 逐块遍历，拼回去必须和原文一致
       let mut rebuilt = String::new();
       for chunk in rope.chunks() {
           rebuilt.push_str(chunk);
       }
       assert_eq!(rebuilt, s);

       println!("len(字节) = {}", rope.len());
       println!("chars(字符) = {}", rope.summary().chars);
       println!("行数 = {}", line_count_rope);
       println!("max_point = {:?}", rope.max_point());
       println!("所有断言通过，Rope 与 String 模型一致");
   }
   ```

3. 运行 `cargo run -p rope --example hello_rope`，观察断言全部通过；然后把 `text` 换成你自己的任意多行文本再跑一次。
4. 最后用 `cargo tree -p rope --depth 1` 对照 4.2.1 的依赖表，确认理解每个依赖的来源。

这个任务覆盖了本讲全部知识点：`Rope::from` 的构建链路、O(1) 统计查询、`chunks()` 的流式消费、以及 crate 的依赖构成。断言的具体数值请以本地输出为准（待本地验证）。

## 6. 本讲小结

- `rope` 是 Zed 最底层的文本存储 crate：文本缓冲区之上的一切（编辑器、diff、协作、LSP 对接）都建立在它提供的存储与坐标系统之上，被 `text`、`editor`、`multi_buffer` 等十多个 crate 依赖。
- `Rope` 的定义只有一行——`chunks: SumTree<Chunk>`：定长小块 + 前缀和树，用 \( O(\log n) \) 的编辑换掉 `String` 的 \( O(n) \) 搬移，同时把字节数、行数、最长行等统计缓存成 \( O(1) \) 查询。
- `len()` 是 UTF-8 字节数，`Point` 的 column 也是字节口径；字符数要看 `summary().chars`。
- `From<&str>` → `push` 的构建链路是「填满尾块 + 按 128 字节切新块，切点回退到 UTF-8 字符边界」。
- 依赖各有分工：`sum_tree`（树骨架）、`heapless`（定长块缓冲）、`rayon`（大文本并行分块）、`unicode-segmentation`（字素边界）；版本统一收口在根 `Cargo.toml` 的 `[workspace.dependencies]`。
- 验证环境的两条命令：`cargo test -p rope` 与 `cargo run -p rope --example hello_rope`。

## 7. 下一步学习建议

下一篇是 u1-l2《源码地图：六个文件如何分工》，将逐个介绍 `chunk.rs`、`point.rs`、`point_utf16.rs`、`offset_utf16.rs`、`unclipped.rs` 的职责边界和 `pub use` 导出关系。在那之前，建议你先自己浏览一遍 [crates/rope/src/rope.rs#L1-L23](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1-L23) 的模块声明，并打开 `src/chunk.rs` 扫一眼 `Chunk` 的字段，带着「四张位图是干嘛的」这个问题进入下一讲。之后 u1-l3 会系统练习 Rope 的日常读写 API。
