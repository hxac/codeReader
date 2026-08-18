# 第一个 Buffer：创建、编辑与读取文本

## 1. 本讲目标

上一讲我们建立了 text crate 的整体地图，知道 `Buffer` 是一个「会记账的文本容器」。本讲我们真正动手使用它。学完本讲，你应该能够：

1. 使用 `Buffer::new` 创建一个缓冲区，并说清三个入参（`replica_id`、`remote_id`、`base_text`）各自代表什么。
2. 使用 `Buffer::edit` 完成插入、删除、替换三类编辑，包括一次调用中传入多个编辑区间。
3. 区分三种「位置写法」：`usize`（可见文本字节偏移）、`FullOffset`（含已删除文本的偏移）、以及作为编辑入参的 `Range<usize>`。
4. 使用 `buffer.text()` 读取文本、`buffer.version()` 观察版本号，并理解 `Buffer::edit` 返回的 `Operation` 为什么可以直接发给其他副本。

本讲刻意停留在**公开 API 层面**：`edit` 内部如何切割 fragment、如何重建 rope，属于第 4 单元（u4-l1）的内容，这里只需要知道「它做了什么」，不需要知道「它怎么做的」。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（不熟悉也没关系，下面用通俗语言解释）：

- **字节偏移（offset）**：文本在内存中是 UTF-8 字节序列，`offset` 就是「从文本开头数到某位置经过了多少个字节」。注意一个中文字符通常占 3 个字节，所以 offset 不等于「第几个字符」。text crate 中裸的 `usize` 作为位置时，指的就是可见文本的字节偏移。
- **副本（replica）**：同一份文档可以同时存在于多个地方（你的编辑器、协作者的编辑器、服务器）。每个副本有一个 `ReplicaId`，用来标识「是谁做的这次编辑」。
- **Lamport 时间戳与版本向量**：为了让多个副本对「编辑先后顺序」达成一致，每次编辑会领取一个全局可比的编号（Lamport 时间戳），而「当前版本」用一个按副本编号计数的向量（`clock::Global`）表示。本讲只需要把它当作「每次编辑后会变化的版本号」来观察，详细机制在 u3-l1 专门讲。
- **事务（transaction）**：一次逻辑编辑的单位，若干个编辑区间可以打包进同一个事务，将来可以作为一个整体被撤销。`edit` 内部会自动开启和关闭事务，本讲只提一句，深入在 u7-l1。
- **`Arc<str>`**：引用计数的字符串切片，多个持有者可以共享同一份文本数据而无需复制。`edit` 的新文本最终以这种形式存储。

## 3. 本讲源码地图

本讲涉及的关键文件与代码位置：

| 文件 | 位置 | 作用 |
| --- | --- | --- |
| `src/text.rs` | `Buffer` 结构体（L59-L68） | 可变缓冲区本体，本讲的主角 |
| `src/text.rs` | `BufferId`（L70-L110） | 缓冲区的非零整数编号 |
| `src/text.rs` | `BufferSnapshot` 结构体（L113-L124） | 只读快照，`text()`、`version()` 真正的归属者 |
| `src/text.rs` | `Operation` / `EditOperation`（L618-L630） | `edit` 的返回值，可广播的编辑操作 |
| `src/text.rs` | `Buffer::new` / `new_normalized`（L748-L827） | 创建缓冲区、为初始文本建立内部结构 |
| `src/text.rs` | `Buffer::version` / `Buffer::edit`（L829-L890） | 本讲的核心 API |
| `src/text.rs` | `Deref for Buffer`（L1907-L1913） | 让 `buffer.text()` 自动转发到快照 |
| `src/text.rs` | `BufferSnapshot::text` 等（L2193-L2211） | 读取文本的一族方法 |
| `src/text.rs` | `ToOffset` trait（L3395-L3430） | 解释「为什么 offset 可以用多种类型传入」 |
| `src/text.rs` | `FullOffset`（L3247-L3248） | 含墓碑文本的偏移类型 |
| `src/tests.rs` | `test_edit`（L17-L31）、`test_concurrent_edits`（L726-L750） | 本讲实践的最佳参照 |
| `../clock/src/clock.rs` | `ReplicaId`（L16-L38）、`Lamport`（L57-L66）、`Global`（L68-L114） | 版本号背后的时钟类型 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**创建（`Buffer::new`）→ 编辑（`Buffer::edit`）→ 读取（`text`）→ 版本（`version`）**。这正好是一条最小使用链路。

### 4.1 模块一：创建缓冲区 —— `Buffer::new`

#### 4.1.1 概念说明

`Buffer::new` 是一切的开始。它的签名需要三个信息：

- **我是谁**（`replica_id: ReplicaId`）：这个缓冲区副本的编号。单机场景下用 `ReplicaId::LOCAL`（值为 0）；协同编辑时每个参与者会拿到不同的编号。
- **这是哪个缓冲区**（`remote_id: BufferId`）：文档级的唯一编号。协同的多个副本共享同一个 `BufferId`，但各有自己的 `ReplicaId`。`BufferId` 内部是 `NonZeroU64`，也就是说 0 不是合法编号——这是一个有意的设计：0 被保留为「空值」语义，让类型系统帮你排除一种常见的错误输入。
- **初始内容是什么**（`base_text: impl Into<String>`）：初始文本，之后的所有编辑都在它之上进行。

#### 4.1.2 核心流程

`Buffer::new` 本身只有三步，真正的工作在 `new_normalized` 里：

```text
Buffer::new(replica_id, remote_id, base_text)
  1. LineEnding::detect(&base_text)    # 记录原文的换行风格（\n、\r\n 还是 \r）
  2. LineEnding::normalize(&mut base_text)  # 把文本内容统一规范化为 \n
  3. new_normalized(...)               # 用规范化后的 Rope 构建完整的 Buffer
       ├─ History::new(base_text)          # 历史记录，撤销/重做的地基
       ├─ 若初始文本非空：
       │    ├─ 生成一个属于 ReplicaId::LOCAL 的初始插入时间戳
       │    ├─ 把初始文本按 MAX_INSERTION_LEN 切成若干 fragment
       │    └─ version.observe(初始时间戳)  # 版本向量从 0 变为记录这次插入
       └─ 组装 Buffer（空的延迟队列、空的订阅、初始时钟）
```

这里有个值得记住的设计：**内部存储永远是 `\n` 换行**，原始风格（比如 Windows 的 `\r\n`）只是作为 `line_ending` 字段记下来，读取时可以按需还原（见 4.3 节的 `text_with_line_endings`）。统一内部表示能极大简化后续所有按行处理的逻辑。

#### 4.1.3 源码精读

先看入口函数，注意它对换行的两步处理：

- [src/text.rs:748-753](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L748-L753)：`Buffer::new` 先用 `LineEnding::detect` 探测换行风格，再用 `LineEnding::normalize` 把文本内容规范化为 `\n`，最后把「风格 + 规范化文本」交给 `new_normalized`。

再看 `BufferId` 的防呆设计：

- [src/text.rs:86-91](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L86-L91)：`BufferId::new` 用 `NonZeroU64::new(id).context("Buffer id cannot be 0.")` 把 0 拒之门外，返回 `anyhow::Result`，调用方必须处理错误。所以测试里随处可见 `BufferId::new(1).unwrap()`。

`new_normalized` 中为初始文本建立内部结构的循环（只看骨架，细节留给 u3-l2）：

- [src/text.rs:768-804](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L768-L804)：初始文本被当作一次「来自 LOCAL 副本的插入」，用一个 `while` 循环按 `MAX_INSERTION_LEN` 上限切成若干 `Fragment` 依次压入 `fragments` 和 `insertions` 两棵树。注意 [src/text.rs:51-55](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L51-L55) 定义了 `MAX_INSERTION_LEN` 在 `cfg!(test)` 下是 **16**（生产环境是 `u32::MAX`），所以测试里超过 16 字节的初始文本会被切成多段——这是读测试时容易困惑的点。
- [src/text.rs:806-826](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L806-L826)：最终组装 `Buffer`。对照 [src/text.rs:59-68](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L59-L68) 的结构体定义可以看到：刚创建的缓冲区里 `history` 只有空的撤销栈、`deferred_ops` 队列为空、`subscriptions` 没有订阅者——「账本」是全新的。

顺带认识两个编号类型（协同语义在 u3-l1 展开）：

- [../clock/src/clock.rs:16-38](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs#L16-L38)：`ReplicaId` 的预定义常量——`LOCAL`(0) 单机本地、`REMOTE_SERVER`(1) 服务器、`AGENT`(2) 智能体、`LOCAL_BRANCH`(3) 本地分支、`FIRST_COLLAB_ID`(8) 起是协作者编号。本讲只用 `LOCAL` 和 `ReplicaId::new(n)`。

#### 4.1.4 代码实践

**实践目标**：验证「创建即规范化」——带 `\r\n` 的文本进入 Buffer 后内部变成 `\n`，但换行风格被记住。

**操作步骤**（在仓库根目录执行，参照 [src/tests.rs:17-31](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L17-L31) 的 `test_edit` 写法，把下面代码临时加到 `src/tests.rs` 末尾，跑完删掉或用 `git checkout -- crates/text/src/tests.rs` 还原）：

```rust
// 示例代码：临时的本地实验测试
#[test]
fn test_new_normalizes_line_endings() {
    let buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "first\r\nsecond\r\n",
    );
    // 内部存储统一为 \n
    assert_eq!(buffer.text(), "first\nsecond\n");
    // 但原始风格被记录
    assert_eq!(buffer.line_ending(), LineEnding::Crlf);
    // 读取时可以还原
    assert_eq!(buffer.text_with_line_endings(), "first\r\nsecond\r\n");
}
```

**需要观察的现象**：`text()` 与 `text_with_line_endings()` 返回不同字符串；`line_ending()` 返回 `LineEnding::Crlf`。

**预期结果**：`cargo test -p text test_new_normalizes_line_endings` 通过。（断言依据来自 4.1.3 引用的 detect/normalize 逻辑与 4.3.3 的 `text_with_line_endings` 实现，建议本地运行确认。）

#### 4.1.5 小练习与答案

**练习 1**：`BufferId::new(0)` 会发生什么？为什么要这样设计？

答案：返回 `Err`，错误信息是 "Buffer id cannot be 0."（见 [src/text.rs:88-89](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L88-L89)）。内部类型是 `NonZeroU64`，0 在很多协议里充当「无值/默认值」，用类型系统直接排除它，比运行时靠人肉检查可靠。

**练习 2**：创建 `Buffer::new(ReplicaId::LOCAL, id, "hello world")` 后，内部为初始文本生成了几个 fragment？

答案：1 个。测试配置下 `MAX_INSERTION_LEN = 16`（[src/text.rs:55](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L55)），"hello world" 共 11 字节，不超过上限，`while` 循环只跑一轮（[src/text.rs:778-803](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L778-L803)）。若初始文本是 20 字节，则会切成 16 + 4 两个 fragment。

**练习 3**：`ReplicaId::LOCAL` 和 `BufferId` 分别回答了什么问题？

答案：`ReplicaId` 回答「这份文档的**哪个副本**在操作」（同一文档的两个编辑器窗口副本不同）；`BufferId` 回答「这是**哪份文档**」（协同的多个副本共享同一个值）。

### 4.2 模块二：执行编辑 —— `Buffer::edit`

#### 4.2.1 概念说明

`Buffer::edit` 是唯一本地修改文本的入口。它一次接收**一批编辑**，每个编辑是一个 `(Range<S>, T)` 二元组：

- `Range<S>`：要替换掉的区间。**区间为空（`x..x`）就是纯插入；新文本为空串就是纯删除；两者都非空就是替换**。
- `T`：替换上去的新文本（任何能转成 `Arc<str>` 的东西，通常是 `&str` 或 `String`）。

返回值是一个 `Operation`——它不是「编辑结果」的包装，而是**这次编辑的完整描述**，拿到它的另一个副本重放（`apply_ops`）之后会得到相同的结果。这就是「返回的 Operation 可以直接发给其他副本」的含义。

#### 4.2.2 核心流程

`edit` 的方法体只有五步，建议把这几行背下来，它是整条编辑主链路的骨架（内部细节在 u4-l1 展开）：

```text
Buffer::edit(edits)
  1. start_transaction()                      # 开启事务（支持嵌套计数）
  2. timestamp = self.lamport_clock.tick()    # 为本次编辑领取 Lamport 时间戳
  3. operation = Operation::Edit(
       self.apply_local_edit(edits, timestamp)  # 真正改数据：
       )                                        #   坐标转 offset → apply_edit_internal
                                                #   → 向订阅者发布 Patch
  4. self.history.push(operation.clone())     # 记入操作历史
     self.history.push_undo(...)              # 记入撤销栈
     self.snapshot.version.observe(timestamp) # 版本向量吸收新时间戳
  5. end_transaction()                        # 关闭事务
  返回 operation
```

一个容易踩的约束：**同一次 `edit` 调用中的多个区间必须按升序排列且互不重叠**。原因是内部的 fragment 游标只会前进（见 4.2.3 最后一条引用），倒序区间无法被正确消费。

#### 4.2.3 源码精读

- [src/text.rs:870-876](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L870-L876)：`edit` 的泛型签名。`S: ToOffset` 意味着区间端点不一定是 `usize`——`Point`（行列）、`PointUtf16`、`Anchor` 都可以，方法内部会先统一转成字节偏移；`T: Into<Arc<str>>` 让你可以直接传 `&str`。`ExactSizeIterator` 约束则允许内部预先按编辑数量分配容量。
- [src/text.rs:881-888](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L881-L888)：上一节流程图的原文，五行对应五步。
- [src/text.rs:892-903](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L892-L903)：`apply_local_edit` 先把所有区间 `to_offset` 成 `usize`，再交给快照的 `apply_edit_internal`，最后把产生的 `Patch` 发布给订阅者（订阅机制在 u5-l3）。
- [src/text.rs:3395-3409](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L3395-L3409)：`ToOffset` trait 定义。它只有一个核心方法 `to_offset(&self, snapshot: &BufferSnapshot) -> usize`，说明任何坐标转换都需要快照参与——同一个 `Point` 在不同版本的缓冲区里对应的 offset 不同。
- [src/text.rs:3418-3430](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L3418-L3430)：`usize` 自己也实现了 `ToOffset`。注意它不是原样返回：debug 模式下会断言落在字符边界上，否则向下取整到最近的字符边界（`floor_char_boundary`）。也就是说传「半个汉字」的偏移不会 panic，而是被吸附到合法位置。
- [src/text.rs:618-630](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L618-L630)：返回值类型。`Operation` 是个枚举（`Edit` 或 `Undo`），`EditOperation` 携带四个字段：`timestamp`（谁、第几笔）、`version`（**编辑前**的版本）、`ranges`（`Vec<Range<FullOffset>>`）、`new_text`。这里出现本讲的第三种位置写法 `FullOffset`。
- [src/text.rs:3247-3248](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L3247-L3248)：`FullOffset(pub usize)` 的定义。它与普通 offset 的区别：普通 `usize` 在**可见文本**上计数，`FullOffset` 在「可见文本 + 墓碑文本」上计数。因为被删除的文本并没有真正离开 buffer（u3-l2 详述），接收方的墓碑区长度可能与发送方不同，用 FullOffset 描述编辑区间才能在任意副本上重放出相同结果。这也解释了 [src/text.rs:1922-1927](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1922-L1927) 中 `EditOperation.version` 为什么要存 `self.version.clone()`（编辑前的版本）：接收方需要知道「这笔编辑是基于哪个历史状态做的」。
- [src/text.rs:1946-1968](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1946-L1968)：`apply_edit_internal` 的主循环开头，`for (range, new_text) in edits` 配合只前进的游标——这就是「区间必须升序且不重叠」的出处。
- [src/tests.rs:17-31](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L17-L31)：`test_edit` 是最好的入门教材，五行 `edit` 分别演示了：末尾插入、开头插入、中间插入、单字符删除（`6..7` 换成空串）、多字符替换（`4..9` 换成 `"mno"`）。

#### 4.2.4 代码实践

**实践目标**：用一次 `edit` 调用同时完成「插入 + 删除」，体会批量编辑的升序约束。

**操作步骤**（同样临时写入 `src/tests.rs`）：

```rust
// 示例代码：一次调用中的多区间编辑
#[test]
fn test_multi_edit_in_one_call() {
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "hello world",
    );
    // 先做一次替换：把 "world"(6..11) 换成 "zed"
    buffer.edit([(6..11, "zed")]);
    assert_eq!(buffer.text(), "hello zed");

    // 再用一次调用同时：开头插入 "> "，末尾删除 "d"
    // "hello zed" 的字节下标：h0 e1 l2 l3 o4 ' '5 z6 e7 d8
    buffer.edit([(0..0, "> "), (8..9, "")]);
    assert_eq!(buffer.text(), "> hello ze");
}
```

**需要观察的现象**：第二个 `edit` 里两个区间的顺序是 `0..0` 在前、`8..9` 在后。试着把两者调换顺序再跑一次。

**预期结果**：按上面升序写法测试通过；调换顺序后行为不符合预期（游标无法回退），这是理解内部约束最直接的方式。（调换后的具体表现建议本地观察，属于「待本地验证」。）

#### 4.2.5 小练习与答案

**练习 1**：想在第 5 个字节处插入 `"abc"`、同时删除 `10..15`，应该怎么写？两个区间的先后由什么决定？

答案：`buffer.edit([(5..5, "abc"), (10..15, "")])`。顺序由区间起点升序决定——起点 5 的编辑必须排在起点 10 之前，与「想先做哪个」无关。

**练习 2**：`edit` 的区间端点可以直接传 `Point::new(0, 5)` 这种行列坐标吗？为什么？

答案：可以。签名约束是 `S: ToOffset`（[src/text.rs:874](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L874)），`Point` 实现了 `ToOffset`（[src/text.rs:3411-3416](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L3411-L3416)），`apply_local_edit` 会先统一转换成 `usize` 偏移再处理。

**练习 3**：`EditOperation.version` 存的是编辑前还是编辑后的版本？为什么必须是那一个？

答案：编辑前的版本（[src/text.rs:1924](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1924) 在构造 `EditOperation` 时克隆的是当时的 `self.version`，而版本吸收新时间戳发生在 `edit` 的第 4 步）。接收方需要用它判断「这笔编辑依赖的状态我是否都见过」，没见过就得先放进延迟队列（u4-l4），所以它必须描述「基于什么」而不是「产生了什么」。

### 4.3 模块三：读取文本 —— `text()` 与 `Deref` 的关系

#### 4.3.1 概念说明

你可能已经注意到：我们一直写 `buffer.text()`，但 `text` 其实定义在 `BufferSnapshot` 上而不是 `Buffer` 上。这靠的是 Rust 的 `Deref` 转发：`Buffer` 内部持有一个 `BufferSnapshot` 字段，编译器自动把 `buffer.text()` 解析为 `buffer.snapshot.text()`。这不是炫技——它准确表达了两个类型的分工：

- `BufferSnapshot`：**纯数据**，保存某一时刻的全部文本状态，可以廉价地 `clone`（内部是持久化数据结构，克隆代价很低）。
- `Buffer`：**数据 + 记账**，在快照之上多了历史、订阅、延迟操作队列、时钟（对照 [src/text.rs:59-68](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L59-L68) 与 [src/text.rs:113-124](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L113-L124) 的字段差异）。

所以「读」走快照、「改」走 Buffer。这个分工是整个 crate 的骨架，u1-l4 会专门展开。

#### 4.3.2 核心流程

```text
buffer.text()
  └─ Deref<Target = BufferSnapshot> ─> &self.snapshot
       └─ BufferSnapshot::text() ─> self.visible_text.to_string()
```

读取一族还有两个近亲：`text_with_line_endings()`（把内部 `\n` 还原成原始风格）和 `deleted_text()`（读墓碑绳——所有「被删掉但还留着」的文本）。

#### 4.3.3 源码精读

- [src/text.rs:1907-1913](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1907-L1913)：`impl Deref for Buffer`，`Target = BufferSnapshot`，直接返回 `&self.snapshot`。这就是 `buffer.text()`、`buffer.version()`、`buffer.len()` 这些「快照方法」能在 Buffer 上直接调用的原因。
- [src/text.rs:113-124](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L113-L124)：`BufferSnapshot` 的字段。本讲只关注前两个：`visible_text: Rope`（可见文本）与 `deleted_text: Rope`（墓碑文本）；`version: clock::Global` 将在 4.4 出场。
- [src/text.rs:2193-2195](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L2193-L2195)：`text()` 就是把可见绳转成 `String`。
- [src/text.rs:2197-2199](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L2197-L2199)：`text_with_line_endings()` 用记录的 `line_ending` 把 `\n` 逐块替换回去——与 4.1 的「创建即规范化」首尾呼应。
- [src/text.rs:2205-2207](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L2205-L2207)：`deleted_text()` 读的是墓碑绳。删除并没有把字节擦掉，而是把对应 fragment 标记为不可见并把文本挪进 `deleted_text`（这正是撤销/协同能「找回」文本的原因，u3-l2 详解）。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「删除的文本没有消失」。

**操作步骤**：

```rust
// 示例代码：观察墓碑文本
#[test]
fn test_deleted_text_is_kept() {
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "abcdef",
    );
    buffer.edit([(2..5, "")]); // 删除 "cde"
    assert_eq!(buffer.text(), "abf");
    assert_eq!(buffer.deleted_text(), "cde"); // 仍可读取
}
```

**需要观察的现象**：`text()` 变短了，但 `deleted_text()` 恰好等于被删的 `"cde"`。

**预期结果**：测试通过。（依据：删除路径把不可见 fragment 的文本推入墓碑绳，见 [src/text.rs:2006-2034](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L2006-L2034) 中 `intersection.visible = false` 及 `new_ropes.push_fragment(&intersection, fragment.visible)` 的分支。）

#### 4.3.5 小练习与答案

**练习 1**：`buffer.text()` 和 `buffer.snapshot().text()` 有区别吗？

答案：没有。前者经 `Deref` 转发到后者（[src/text.rs:1907-1913](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L1907-L1913)），是完全等价的写法；显式写 `snapshot()` 的价值在于把快照 `clone` 出去长期持有。

**练习 2**：为什么 `BufferSnapshot` 可以设计成 `#[derive(Clone)]` 随意克隆，而 `Buffer` 不行？

答案：快照里全是不可变数据（两根绳、三棵树、版本向量），底层是持久化数据结构，克隆只是增加引用；而 `Buffer` 持有撤销栈、订阅者、待处理回调等**会继续演化的记账状态**，克隆它意味着分叉出两套互不知情的历史，语义上就是错的。

**练习 3**：`deleted_text()` 在什么场景下有用？

答案：任何需要「找回」或「审视」已删内容的场景：撤销/重做（u7-l2）、协同时对端撤销广播、以及调试时观察墓碑区状态。

### 4.4 模块四：观察版本 —— `Buffer::version` 与 `clock::Global`

#### 4.4.1 概念说明

每次编辑后，除了文本变化，还有一个东西在悄悄前进：**版本号**。`buffer.version()` 返回 `clock::Global`（一个版本向量）：

- 它内部是一个按 `ReplicaId` 索引的计数数组（[../clock/src/clock.rs:69-74](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs#L69-L74) 的 `SmallVec<[u32; 4]>`）。
- `values[r] = n` 的含义是：「本副本已经见到副本 `r` 的前 `n` 笔操作」。
- 单机场景下只有 `LOCAL`（0 号）在涨；协同场景下每个协作者的编号各占一格。

为什么用「向量」而不是单个计数器？因为副本之间消息可能乱序到达，单一序号无法表达「我见过 1、3 但没见过 2」这种部分进度。这个设计是 u3-l1 的主题，本讲先把它当作观察编辑历史的仪表盘。

#### 4.4.2 核心流程

版本的推进只由 `observe` 驱动，规则很简单：

\[ \text{values}[r] \leftarrow \max\left(\text{values}[r],\; t_{\text{value}}\right) \]

即「看到副本 `r` 的第 \( t_{\text{value}} \) 笔操作后，把对 `r` 的认识至少更新到这个数」。结合 4.2 的 `edit` 流程，可以推出本讲实验中版本号的精确变化（`LOCAL` 即 0 号副本）：

| 时刻 | 事件 | `version.get(LOCAL)` |
| --- | --- | --- |
| `Buffer::new(..., "hello world")` | 初始文本被视作 LOCAL 的第 1 笔插入并被 observe（[src/text.rs:770-772](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L770-L772)） | 1 |
| 第一次 `edit`（`tick` 领到 value=2 的时间戳，编辑后 observe） | 见 [src/text.rs:882](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L882)、[src/text.rs:887](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L887) | 2 |
| 第二次 `edit` | 同上 | 3 |

推导链：`Lamport::new` 起始 value 为 1（[../clock/src/clock.rs:240-245](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs#L240-L245)）；创建非空缓冲区时本地时钟 `observe` 了这次初始插入，变为 2（[../clock/src/clock.rs:257-259](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs#L257-L259)：`max(1,1)+1=2`）；`tick` 返回当前值再自增（[../clock/src/clock.rs:251-255](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs#L251-L255)），所以第一、二次编辑的时间戳分别是 2、3，被版本向量吸收后 `get(LOCAL)` 依次为 2、3。

#### 4.4.3 源码精读

- [src/text.rs:829-831](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L829-L831)：`Buffer::version` 返回 `clock::Global` 的克隆（快照上还有个借用的版本 [src/text.rs:2269-2271](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L2269-L2271) 返回 `&clock::Global`）。克隆版本号是「记录检查点」的惯用法，之后的 `edits_since(checkpoint)` 会用到（u5-l1）。
- [../clock/src/clock.rs:96-98](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs#L96-L98)：`Global::get(replica_id)` 查询某副本的进度，是打印观察最顺手的入口。
- [../clock/src/clock.rs:103-114](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs#L103-L114)：`Global::observe` 的实现，就是 4.4.2 那条 max 公式的代码形态。
- [../clock/src/clock.rs:57-66](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs#L57-L66)：`Lamport` 时间戳 = `(value: Seq, replica_id)`，两字段合起来才能全局定序。
- [src/tests.rs:726-750](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L726-L750)：`test_concurrent_edits`——本讲「Operation 可以发给别的副本」的实证。三个副本各自 `edit` 拿到 `buf1_op/buf2_op/buf3_op`，然后互相 `apply_op`，最终三个副本的 `text()` 收敛为同一个 `"a12c34e56"`。注意测试能调用私有的 `apply_op` 是因为 `mod tests` 就在 crate 内部（[src/text.rs:9-10](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L9-L10)）；crate 外部的使用者应调用公开的 `apply_ops`（[src/text.rs:909-922](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L909-L922)）。

#### 4.4.4 代码实践

**实践目标**：把版本号变成肉眼可见的编辑计数器。

**操作步骤**：

```rust
// 示例代码：观察版本号随编辑推进
#[test]
fn test_version_advances() {
    use clock::ReplicaId;
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "hello world",
    );
    println!("after new:    {:?}", buffer.version()); // Debug 输出
    assert_eq!(buffer.version().get(ReplicaId::LOCAL), 1);

    buffer.edit([(6..11, "zed")]);
    println!("after edit 1: {:?}", buffer.version());
    assert_eq!(buffer.version().get(ReplicaId::LOCAL), 2);

    buffer.edit([(0..0, "> "), (8..9, "")]);
    println!("after edit 2: {:?}", buffer.version());
    assert_eq!(buffer.version().get(ReplicaId::LOCAL), 3);
}
```

**需要观察的现象**：三行 `println!` 输出的 `Global` 值单调递增（形如 `[1]` → `[2]` → `[3]`，实际格式以 `Debug` 实现为准）。

**预期结果**：断言全部通过——数字推导见 4.4.2 的表格；`Global` 实现了 `Debug`（[../clock/src/clock.rs:274](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs#L274)），但**没有实现 `Display`**，所以要用 `{:?}` 打印。具体打印样式建议本地运行确认。

#### 4.4.5 小练习与答案

**练习 1**：两次编辑之后 `version.get(ReplicaId::LOCAL)` 是多少？如果创建的是**空文本**缓冲区呢？

答案：非空 "hello world"：创建后为 1，两次编辑后为 3（推导见 4.4.2）。空文本时初始插入不存在（[src/text.rs:769](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L769) 的 `if !visible_text.is_empty()` 不成立），时钟停在 1，第一、二次编辑的时间戳分别为 1、2，所以版本依次是 0 → 1 → 2。

**练习 2**：`buffer.version()` 与 `buffer.snapshot().version()` 返回类型有什么不同？各自适合什么场景？

答案：前者返回拥有的 `clock::Global`（[src/text.rs:829-831](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L829-L831)），适合存成检查点；后者返回引用（[src/text.rs:2269-2271](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/text.rs#L2269-L2271)），适合当场比较，但生命周期绑定在快照借用上。

**练习 3**：为什么 `test_concurrent_edits` 里三个副本互相转发 `Operation` 后文本能完全一致，而不是出现三种不同的交错结果？

答案：每笔 `EditOperation` 携带编辑者的 `timestamp` 与编辑前 `version`，重放时并发插入的先后由 Lamport 时间戳统一裁决（平局规则在 u4-l3 精读），因此无论操作以什么顺序到达，所有副本都收敛到同一个确定的结果。

## 5. 综合实践

把本讲四个模块串成一个完整的测试。任务：管理一个 `Buffer` 的生命周期——创建、替换、批量编辑、每一步校验文本与版本，最后再体验一次「Operation 发给另一个副本」。

将下面的测试临时添加到 `src/tests.rs` 末尾（它综合了 4.1–4.4 的全部知识点；跑完后用 `git checkout -- crates/text/src/tests.rs` 还原，不要把实验代码留在源码里）：

```rust
// 示例代码：本讲综合实践
#[test]
fn test_my_first_buffer_journey() {
    // 模块一：创建（记下初始版本）
    let mut buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "hello world",
    );
    let v0 = buffer.version();
    assert_eq!(v0.get(ReplicaId::LOCAL), 1);

    // 模块二(替换) + 模块三(读取) + 模块四(版本)
    let op1 = buffer.edit([(6..11, "zed")]); // "world" -> "zed"
    assert_eq!(buffer.text(), "hello zed");
    let v1 = buffer.version();
    assert_eq!(v1.get(ReplicaId::LOCAL), 2);

    // 模块二(一次调用多区间：插入前缀 + 删除后缀)
    let op2 = buffer.edit([(0..0, "> "), (8..9, "")]);
    assert_eq!(buffer.text(), "> hello ze");
    let v2 = buffer.version();
    assert_eq!(v2.get(ReplicaId::LOCAL), 3);

    // 打印观察版本向量与操作内容
    println!("v0 = {:?}, v1 = {:?}, v2 = {:?}", v0, v1, v2);
    println!("op1 timestamp = {:?}", op1.timestamp());
    println!("op2 = {:#?}", op2);

    // 延伸：把这两笔操作重放到同源的另一个副本上
    let mut replica = Buffer::new(ReplicaId::new(2), BufferId::new(1).unwrap(), "hello world");
    replica.apply_ops([op1, op2]);
    assert_eq!(replica.text(), "> hello ze");
    assert_eq!(replica.text(), buffer.text());
}
```

预期结果（按源码推导，建议本地运行验证）：

1. 三次版本断言依次为 1、2、3（推导见 4.4.2）。
2. `op1.timestamp()` 的 value 为 2、`op2` 的为 3，且 replica_id 都是 LOCAL。
3. 重放副本的文本与原副本一致——这就是 `Operation` 作为「可广播编辑描述」的意义。
4. 运行命令：`cargo test -p text test_my_first_buffer_journey -- --nocapture`（`--nocapture` 让 `println!` 可见）。

## 6. 本讲小结

- `Buffer::new(replica_id, remote_id, base_text)` 三个入参分别回答「哪个副本」「哪份文档」「初始内容」；创建时换行被统一规范化为 `\n`，原始风格记在 `line_ending` 里。
- `Buffer::edit` 接收 `(Range<S>, T)` 迭代器，空区间即插入、空文本即删除；同一次调用中的区间必须**升序且不重叠**。
- 位置有三种写法：`usize`（可见文本字节偏移，`edit` 的常用入参）、`FullOffset`（含墓碑文本的偏移，`EditOperation.ranges` 使用的坐标系）、`Range<usize>`（编辑区间的形态）。
- `buffer.text()` 实际是 `BufferSnapshot::text` 经 `Deref` 转发——「读走快照、改走 Buffer」是本 crate 的基本分工。
- `buffer.version()` 返回版本向量 `clock::Global`，每个本地编辑让它对 LOCAL 的计数加一；版本是后续 `edits_since` 差分查询的检查点。
- `edit` 返回的 `Operation` 携带时间戳、编辑前版本与 FullOffset 区间，发给其他副本重放即可收敛到相同文本。

## 7. 下一步学习建议

- 下一讲（u1-l3）会打开 `src/` 下全部十个文件，绘制模块依赖图，把本讲提到的「历史、订阅、延迟队列」落到具体文件上。
- 想先巩固本讲的话，推荐精读 [src/tests.rs:17-31](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L17-L31)（`test_edit`）并自己变化区间做实验，再看 [src/tests.rs:726-750](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/src/tests.rs#L726-L750)（`test_concurrent_edits`）体会操作转发。
- 对版本向量感到意犹未尽的读者，可以直接翻 [../clock/src/clock.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/text/../clock/src/clock.rs)，它只有约 300 行，是 u3-l1 的全部准备材料。
