# Rope 上手：构建、读取与修改文本

## 1. 本讲目标

学完本讲，你应该能够：

1. 熟练使用 `Rope` 的日常读写 API：`new` / `From<&str>` / `push` / `push_front` / `append` / `replace` / `slice` / `slice_rows`。
2. 用 `len` / `summary` / `max_point` / `is_empty` 以 \( O(1) \) 复杂度读取文本统计信息，并理解 **byte offset（字节偏移）是所有操作的基准坐标**。
3. 用 `chars` / `chunks` / `bytes_in_range` 等迭代器以 `char` 或 `&str` 为单位消费文本，而不把整根绳子物化成 `String`。
4. 理解 `Display` / `Debug` 的输出行为，知道为什么 `Rope` 没有提供 `as_str()` 这样的整串借用。

本讲只讲「怎么用」以及「为什么这样设计」；`push` 背后的分块策略细节、`Cursor` 的高级用法、坐标换算家族分别属于 u2-l6、u2-l7、u2-l8。

## 2. 前置知识

本讲承接 u1-l1 和 u1-l2 的结论，这里只做最小回顾：

- `Rope` 内部只有一个字段 `chunks: SumTree<Chunk>`（[src/rope.rs:L25-L28](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L25-L28)）：文本被切成不超过 `MAX_BASE`（生产环境为 128）字节的小块 `Chunk`，挂在前缀和树 `SumTree` 上。树根缓存了全树的摘要，所以「整根绳子有多少字节 / 多少行」这类问题不需要遍历。
- `TextSummary` 是一段文本的统计快照（字节数、字符数、行列等），本讲只用到它的少数字段，u2-l2 会逐字段展开。
- `Point` 是「行:列」坐标，其中 **column 按字节计数**（u2-l1 详讲）。

另外需要两点半懂不懂的 Rust 基础：

- **UTF-8 是变长编码**：一个 Unicode 字符占 1～4 字节（`'a'` 1 字节、`'中'` 3 字节、`'🦀'` 4 字节）。`str` 的任意切片必须落在「字符边界」（char boundary）上，否则 `&text[i..j]` 会 panic。这个约束会被 `Rope` 原样继承——这是本讲最重要的安全话题。
- **`Range<usize>`**：即 `a..b` 这样的左闭右开区间，`Rope` 的所有区间参数都是「字节偏移区间」。

## 3. 本讲源码地图

本讲几乎全部内容来自 [src/rope.rs](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs)（共 2518 行，后半部分是测试）。可以按下表分区定位：

| 行号范围 | 内容 | 与本讲的关系 |
| --- | --- | --- |
| L25-L102 | `Rope` 结构体、字符边界检查（`is_char_boundary` 等） | 本讲的安全契约 |
| L104-L145 | `append` / `replace` / `slice` / `slice_rows` | 修改与切片 API |
| L147-L310 | `push` / `push_large` / `push_chunk` / `push_front` / `check_invariants` | 写入路径 |
| L312-L367 | `summary` / `len` / `max_point` / `chars` / `chunks` / `bytes_in_range` | 读取与遍历 API |
| L369-L619 | 各类坐标换算与 clip | u2-l1 / u2-l8 的地盘，本讲跳过 |
| L621-L676 | `From<&str>` 等转换 trait、`Display` / `Debug` | 构建与打印 |
| L678-L795 | `Cursor` | 本讲只在 `slice`/`replace` 内部遇到它 |
| L797-L1253 | `Chunks` / `Bytes` / `Lines` 迭代器 | 遍历的底层实现 |
| L1281-L1439 | `TextSummary` | 统计信息的来源 |
| L1728-L2518 | `mod tests` | 实践环节的参照 |

辅助引用一个文件：[src/chunk.rs:L8-L14](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L8-L14) 定义了块大小常量：生产环境 `Bitmap = u128`，因此 `MAX_BASE = 128`、`MIN_BASE = 64`；测试配置下 `Bitmap` 缩为 `u16`（块最多 16 字节），这是为了让小文本也能触发多块逻辑。

## 4. 核心概念与源码讲解

### 4.1 构建 Rope：`new`、`From<&str>` 与 `push`

#### 4.1.1 概念说明

把文本放进 `Rope` 有四条路：`Rope::new()` 建空绳子、`Rope::from("...")` 从字符串整体构建、`rope.push("...")` 追加到尾部、`rope.push_front("...")` 插到头部。它们最终都汇入同一条写入路径——「填满尾块，放不下就切新块」。

为什么 `From<&str>` 只是 `push` 的一层薄封装？因为构建一根绳子本质上就是「把文本按字符边界切成 ≤128 字节的块，逐块挂到 `SumTree` 上」，没有任何更魔法的捷径。

#### 4.1.2 核心流程

`rope.push(text)` 的执行过程（伪代码）：

```text
若 text 为空 → 直接返回
1. 填充尾块（update_last）：
   若 尾块现有字节 + text 全部 ≤ 128 → 整段塞进尾块，结束
   否则 切点 = min(64 - 尾块现有字节, text.len())
        切点右移（+1，最多 3 次）直到落在字符边界
        把 [0, 切点) 塞进尾块，text 剩下 remainder
2. 若 remainder 为空 → 结束
3. 若 remainder 很长（生产环境 > 4*128 - 4*4 = 496 字节）→ 走 push_large
4. 否则进入切块循环：
   切点 = min(128, text.len())
   切点左移（-1，最多 3 次）直到落在字符边界
   切出一块，挂到树上，继续处理剩余
```

注意步骤 1 里切点**右移**（`+1`）而步骤 4 里切点**左移**（`-1`）：填充尾块时的目标只是把尾块填到至少 `MIN_BASE`（64 字节），离 128 的硬上限还有余量，右移不越界；而切块循环的起点就是 128，块大小绝不能超过上限，只能左移回退到字符边界。

#### 4.1.3 源码精读

先看 `new` 和 `From` 家族——它们极其朴素：

- [src/rope.rs:L31-L33](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L31-L33)：`Rope::new()` 直接返回 `Self::default()`（`Rope` 派生了 `Default`，空树）。
- [src/rope.rs:L621-L627](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L621-L627)：`From<&str>` 就是「new 一个空绳子再 push」，没有任何特殊优化。
- [src/rope.rs:L629-L651](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L629-L651)：`FromIterator<&str>` 逐段 push；`From<String>` 和 `From<&String>` 借出 `&str` 后转调 `From<&str>`。

再看 `push` 的主体，关键是先填尾块再切块的两段式结构：

```rust
pub fn push(&mut self, mut text: &str) {
    self.chunks.update_last(
        |last_chunk| {
            let split_ix = if last_chunk.text.len() + text.len() <= chunk::MAX_BASE {
                text.len()
            } else {
                let mut split_ix = cmp::min(
                    chunk::MIN_BASE.saturating_sub(last_chunk.text.len()),
                    text.len(),
                );
                while !text.is_char_boundary(split_ix) {
                    split_ix += 1;
                }
                split_ix
            };

            let (suffix, remainder) = text.split_at(split_ix);
            last_chunk.push_str(suffix);
            text = remainder;
        },
        (),
    );
```

这是 [src/rope.rs:L147-L168](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L147-L168)：`update_last` 定位到树上最后一个块并原地修改它；`split_ix` 决定这段 text 有多少字节可以并入尾块。若尾块装不下全部，就把尾块至少填到 `MIN_BASE`，并用 `while !text.is_char_boundary(split_ix)` 把切点推到字符边界。

之后是「太长走大流量路径、否则定长数组切块」：

```rust
#[cfg(all(test, not(rust_analyzer)))]
const NUM_CHUNKS: usize = 16;
#[cfg(not(all(test, not(rust_analyzer))))]
const NUM_CHUNKS: usize = 4;

if text.len() > NUM_CHUNKS * chunk::MAX_BASE - NUM_CHUNKS * 4 {
    return self.push_large(text);
}
let mut new_chunks = ArrayVec::<_, NUM_CHUNKS, u8>::new();

while !text.is_empty() {
    let mut split_ix = cmp::min(chunk::MAX_BASE, text.len());
    while !text.is_char_boundary(split_ix) {
        split_ix -= 1;
    }
    let (chunk, remainder) = text.split_at(split_ix);
    new_chunks.push(chunk).unwrap();
    text = remainder;
}
self.chunks.extend(new_chunks.into_iter().map(Chunk::new), ());
```

见 [src/rope.rs:L175-L201](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L175-L201)。两个细节：

- `NUM_CHUNKS * 4` 的裕量：最坏情况下每个块末尾都可能被一个 4 字节字符「顶开」最多 3 字节（字符被推到下一块），所以阈值比 `NUM_CHUNKS * MAX_BASE` 再减去 `NUM_CHUNKS * 4`，保证 `ArrayVec`（栈上定长数组，来自 `heapless`）不会溢出——`push(chunk).unwrap()` 才敢用 `unwrap`。
- 每切出一段 `&str`，就由 `Chunk::new` 构造一个带位图的块（位图细节在 u2-l4）。

超过阈值时转投 `push_large`（[src/rope.rs:L205-L246](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L205-L246)）：它预先 `div_ceil(MAX_BASE - 3)` 估算块数、一次性分配堆 `Vec`，切块逻辑相同；当块数超过 `PARALLEL_THRESHOLD`（生产环境为 `84 * (2 * sum_tree::TREE_BASE)`）时用 rayon 的 `par_extend` **并行**构造所有块。这就是「打开大文件」场景的写入路径。

#### 4.1.4 代码实践

1. **实践目标**：直观感受「`From<&str>` 就是 push」，并验证多字节文本的 `len` 与直觉的差异。
2. **操作步骤**（源码阅读 + 小实验，延续 u1-l1 建好的独立小程序，示例代码）：

   ```rust
   use rope::Rope;

   fn main() {
       let mut rope = Rope::new();          // 空绳子
       rope.push("hello");                   // 填入第一个块
       rope.push(" world");                  // 并入尾块
       rope.push(" 你好");                   // 多字节字符
       println!("len = {}", rope.len());     // 字节数
       println!("chars = {}", rope.summary().chars);
       println!("{}", rope);                 // Display 输出全文
   }
   ```

3. **需要观察的现象**：`len` 打印的是**字节数**而不是字符数。
4. **预期结果**：`"hello world 你好"` 共 10 个字母（各 1 字节）+ 2 个空格（各 1 字节）+ 2 个汉字（各 3 字节）= 18，因此 `len = 18`；`chars = 14`（12 个 ASCII 字符 + 2 个汉字）。若与你的手算不一致，重新数字节。完整运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Rope::from("你好🦀")` 的 `len()`、`summary().chars`、`summary().len_utf16` 各是多少？

**答案**：`len()` = 3 + 3 + 4 = **10**（字节）；`chars` = **3**；`len_utf16` = 1 + 1 + 2 = **4**——`'🦀'`（U+1F980）超出 BMP，需要一对 UTF-16 代理项，占 2 个 code unit。

**练习 2**：为什么填充尾块时切点越界用 `+1` 前进、切块循环里却用 `-1` 后退？

**答案**：尾块填充的目标是 `MIN_BASE`（64），距 `MAX_BASE`（128）还有很大余量，右移最多 3 字节不会超限；切块循环的起点切点就是 `min(MAX_BASE, len)`，块大小是硬上限，只能回退。且 `split_at` 回退后前缀仍是合法 `&str`（回退到边界为止）。

**练习 3**：生产环境下多大的 `push` 会进入 `push_large`？

**答案**：`text.len() > NUM_CHUNKS * MAX_BASE - NUM_CHUNKS * 4`，其中 `NUM_CHUNKS = 4`、`MAX_BASE = 128`，即超过 **496 字节**（见 [src/rope.rs:L175-L185](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L175-L185)）。

### 4.2 修改文本：`push_front`、`append`、`replace`、`slice`、`slice_rows`

#### 4.2.1 概念说明

`Rope` 的修改 API 分两类：

- **拼接类**：`push_front`（头部插入）、`append`（拼接另一根绳子）。它们尽量复用已有的树结构，是 \( O(\log n) \) 级别的操作。
- **重构类**：`replace`（把一段区间替换为新文本）、`slice`（取子绳）、`slice_rows`（按行取子绳）。它们的实现思路是「用 `Cursor` 把原绳拆成前缀、后缀和中间三段，丢弃中间段、插入新文本，再拼回来」。

「编辑器里敲一个字」在底层就是一次 `replace`——这也是 rope 数据结构存在的意义：`String` 的中间插入要搬移后半段全部字节，`Rope` 只重建路径上的少数节点。

#### 4.2.2 核心流程

`replace(range, text)` 的流程（这是理解本讲所有修改 API 的钥匙）：

```text
new_rope = 空绳
cursor   = self.cursor(0)          // 游标指到原绳开头
new_rope.append(cursor.slice(range.start))  // [0, range.start) → 前缀
cursor.seek_forward(range.end)              // 跳过被替换区间
new_rope.push(text)                        // 插入新文本
new_rope.append(cursor.suffix())            // [range.end, len) → 后缀
*self = new_rope
```

`slice(range)` 是同一套动作去掉「插入」步骤；`slice_rows(rows)` 则先把行号换算成字节偏移再转调 `slice`。

#### 4.2.3 源码精读

- [src/rope.rs:L124-L132](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L124-L132)：`replace` 的全部实现只有 8 行，与上面的伪代码一一对应。注意它**不是原地修改树**，而是构建一根新绳再整体替换 `*self`——被复用的块通过 `Cursor::slice` / `append` 在树层面共享，不需要拷贝文本。

- [src/rope.rs:L134-L138](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L134-L138)：`slice` 同样是「cursor 定位起点，再切到终点」。

- [src/rope.rs:L140-L145](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L140-L145)：`slice_rows(range: Range<u32>)` 把「第 start 行行首」和「第 end 行行首」经 `point_to_offset` 换算成字节偏移，再走 `slice`。例如 `slice_rows(1..3)` 取的是第 1、2 两行的完整内容（含第 2 行末尾的换行符）。

- [src/rope.rs:L276-L296](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L276-L296)：`push_front` 的三分支——空文本直接返回；空绳子转调 `push`；若首块装得下（`first_chunk.text.len() + text.len() <= MAX_BASE`）就 `update_first` + `prepend_str` 原地前插；否则把 `self` 整体换成一根新绳（`mem::replace`），再把旧内容 `append` 到后面。

- [src/rope.rs:L104-L122](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L104-L122)：`append` 的合并策略——如果「我方尾块或对方首块有一方小于 `MIN_BASE`」，就把对方首块经 `push_chunk` 倒进我方尾块（触发块合并，避免树上出现碎块），再从对方第二个块起拼接后缀；否则直接 `SumTree::append` 两棵树。

顺带一提，`Cursor::slice`（[src/rope.rs:L711-L743](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L711-L743)）能高效切出子绳，靠的是 `ChunkSlice` 对块的零拷贝切片（`start_chunk.slice(start_ix..end_ix)`）和 `SumTree` 游标的 `slice`——中间整段完整的块直接被树结构共享，不复制任何文本。

#### 4.2.4 代码实践

1. **实践目标**：用 `replace` / `slice` / `slice_rows` 完成一次「编辑器式」编辑，并对照 `String` 的等价操作。
2. **操作步骤**（示例代码）：

   ```rust
   use rope::Rope;

   fn main() {
       let mut rope = Rope::from("hello world\nsecond line\nthird line\n");
       // 等价于 String 的 replace_range(6..11, "zed")
       rope.replace(6..11, "zed");
       println!("{}", rope);

       // 取第 1 行（行号从 0 计，结果为 "second line\n"）
       let rows = rope.slice_rows(1..2);
       println!("row 1 = {:?}", rows);

       // 取前 5 个字节
       println!("slice = {:?}", rope.slice(0..5).to_string());
   }
   ```

3. **需要观察的现象**：`replace` 后第一行变成 `hello zed`；`slice_rows(1..2)` 取出的是**第 1 行**（行号从 0 计）。
4. **预期结果**：输出第一段为 `hello zed\nsecond line\nthird line\n`；`rows` 的调试输出为 `"second line\n"`（注意 `slice_rows(1..2)` 的范围是「第 1 行行首到第 2 行行首」，即第 1 行全文**含末尾换行符**，行号从 0 计）；`slice` 打印 `"hello"`。完整运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：对 `Rope::from("hello world")` 执行 `replace(2..8, "x")`，结果文本和 `len()` 是什么？

**答案**：区间 `[2, 8)` 覆盖 `"llo w"`（下标 2～7），替换后为 `"he" + "x" + "orld"` = `"hexorld"`，`len()` = 2 + 1 + 3 = **6**。

**练习 2**：`replace(1..2, "x")` 作用在 `Rope::from("你好")` 上会发生什么？

**答案**：**panic**。`'你'` 占 3 字节，字节 1 和 2 都不是字符边界。`replace` 内部经 `Cursor::slice` 走到 `ChunkSlice::split_at`，最终 `str::split_at`（[src/chunk.rs:L250-L280](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/chunk.rs#L250-L280) 中的 `self.text.split_at(mid)`）在非边界下标上 panic。所有区间 API 都要求端点落在字符边界——先用 `is_char_boundary` / `clip_offset` 校验（u3-l1 详讲防御体系）。

**练习 3**：`push_front(" ")` 作用在 `Rope::from("hint")` 上的预期结果是什么？哪个测试覆盖了它？

**答案**：结果为 `" hint"`、`len() == 5`——这正是 [src/rope.rs:L2411-L2416](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L2411-L2416) 的 `test_push_front_single_space`；空绳上的 `push_front` 行为由 [src/rope.rs:L2402-L2408](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L2402-L2408) 的 `test_push_front_on_empty_rope` 覆盖（断言 `max_point() == Point::new(0, 5)`）。

### 4.3 O(1) 统计读取：`len`、`summary`、`max_point` 与基准坐标

#### 4.3.1 概念说明

三个只读方法回答「这根绳子有多大」：

| 方法 | 返回 | 含义 | 复杂度 |
| --- | --- | --- | --- |
| `len()` | `usize` | **UTF-8 字节数** | \( O(1) \) |
| `summary()` | `TextSummary` | 全部统计快照 | \( O(1) \) |
| `max_point()` | `Point` | 末尾位置（行数、末行字节长） | \( O(1) \) |
| `is_empty()` | `bool` | `len() == 0` | \( O(1) \) |

它们快的理由只有一个：`SumTree` 的树根缓存了全树摘要，`Rope` 只是把这个缓存转发出来。

本节还要建立一个贯穿全 crate 的心智模型：**byte offset 是基准坐标**。`push` 的切点、`slice`/`replace`/`chunks_in_range`/`bytes_in_range` 的区间参数、`Cursor` 的定位参数，全部是字节偏移；字符数（`chars`）、UTF-16 偏移、行列 `Point` 都是「衍生坐标」，需要经换算函数从字节偏移导出（u2-l1 / u2-l8）。这带来的直接后果是上一节练习 2 展示的：**给区间 API 传非字符边界会 panic**。

#### 4.3.2 核心流程

统计信息从哪里来：

```text
push/replace 等写入操作
  → 每个块在被创建时算出自己的 TextSummary（Chunk::new 构造位图时顺便统计）
  → SumTree 在插入/拼接时自底向上把摘要累加到树根
读取 len()/summary()/max_point()
  → 直接读树根缓存 → O(1)
```

`TextSummary` 的九个字段定义在 [src/rope.rs:L1281-L1304](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1281-L1304)，本讲只用 `len`（字节数）、`chars`（字符数）、`lines`（末尾 `Point`）；`first_line_chars`、`longest_row` 等留到 u2-l2。

#### 4.3.3 源码精读

三个方法都只有一行，且都转发给树根：

```rust
pub fn summary(&self) -> TextSummary {
    self.chunks.summary().text
}

pub fn len(&self) -> usize {
    self.chunks.extent(())
}

pub fn is_empty(&self) -> bool {
    self.len() == 0
}

pub fn max_point(&self) -> Point {
    self.chunks.extent(())
}
```

见 [src/rope.rs:L312-L330](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L312-L330)。`extent` 是 `SumTree` 在某个维度上的总和——`len()` 取的是 `usize`（字节）维度，`max_point()` 取的是 `Point` 维度，同一个树根、两种投影。`chunks.summary().text` 里的 `.text` 是把 `ChunkSummary`（[src/rope.rs:L1265-L1268](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1265-L1268)）剥壳取出内嵌的 `TextSummary`。

字符边界契约的两个守门方法（本讲了解即可，u3-l1 详讲）：

- [src/rope.rs:L42-L50](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L42-L50)：`is_char_boundary(offset)` 先用 `find` 在树上定位到 offset 所在的块（\( O(\log n) \)，不是 \( O(1) \)），再由块内位图判断。
- [src/rope.rs:L536-L541](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L536-L541)：`clip_offset(offset, bias)` 把任意下标吸附到左边（`Bias::Left` → `floor_char_boundary`）或右边（`Bias::Right` → `ceil_char_boundary`）最近的字符边界。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证 `len` 与 `chars` 的差异，并体验一次「越界下标被吸附」。
2. **操作步骤**（示例代码）：

   ```rust
   use rope::Rope;
   use sum_tree::Bias;

   fn main() {
       let rope = Rope::from("a中🦀");            // 1 + 3 + 4 = 8 字节
       println!("len = {}", rope.len());           // 8
       println!("chars = {}", rope.summary().chars); // 3
       println!("max_point = {:?}", rope.max_point()); // Point { row: 0, column: 8 }

       // 字节 2 在 '中'（占 1..4）内部，向右吸附到 4
       println!("{:?}", rope.clip_offset(2, Bias::Right)); // 4
       println!("{:?}", rope.clip_offset(2, Bias::Left));  // 1
   }
   ```

3. **需要观察的现象**：`clip_offset(2, Right)` 返回 4（下一个边界），`clip_offset(2, Left)` 返回 1（上一个边界）。
4. **预期结果**：如注释所示。`Bias` 需要额外依赖 `sum_tree`（它是 rope 的公开依赖，workspace 里有）。完整运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`Rope::from("a\nbb\nccc")` 的 `max_point()`、`summary().first_line_chars`、`summary().longest_row` 各是什么？

**答案**：`max_point() = Point { row: 2, column: 3 }`（2 个换行后进入第 2 行，末行 3 字节）；`first_line_chars = 1`；`longest_row = 2`、`longest_row_chars = 3`（第 2 行 3 个字符最长，字段语义见 [src/rope.rs:L1294-L1303](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1294-L1303)）。

**练习 2**：为什么 `len()` 是 \( O(1) \) 而 `is_char_boundary()` 不是？

**答案**：`len()` 读的是树根缓存的 `extent`；`is_char_boundary()` 必须先知道 offset 落在**哪个块**的哪个下标，需要一次 \( O(\log n) \) 的树查找（[src/rope.rs:L46](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L46) 的 `find`）。

**练习 3**：`Rope::from("")` 的 `is_empty()`、`len()`、`max_point()` 分别是什么？

**答案**：`true`、`0`、`Point { row: 0, column: 0 }`（空树的 `extent` 为零值）。

### 4.4 遍历文本：`chunks`、`chars`、`bytes_in_range` 与 `Display`/`Debug`

#### 4.4.1 概念说明

`Rope` 有意**不提供** `as_str()` 或 `Deref<Target = str>`：一根绳子可能几十 MB，把它整体借成 `&str` 会强迫块之间连续，rope 就退化回 `String` 了。消费文本的正确姿势是按迭代器分块处理：

| 迭代器 / 方法 | 产出 | 适用场景 |
| --- | --- | --- |
| `chunks()` | `&str`（每段 ≤128 字节） | 最高效的全文扫描，配合 `str` 的方法 |
| `chunks_in_range(a..b)` | `&str` | 只扫一个区间 |
| `chars()` / `chars_at(n)` | `char` | 逐字符处理 |
| `bytes_in_range(a..b)` | `&[u]8`，还实现 `io::Read` | 字节级 / 流式写出 |
| `reversed_*` 系列 | 同上（反向） | 从后往前找 |

所有 `&str` 都是直接借用块内存的**零拷贝切片**，没有分配。

#### 4.4.2 核心流程

`Chunks` 迭代器的工作方式（正向）：

```text
构造：游标 seek 到 range.start（必须落在字符边界，否则 assert panic）
next()：
  peek() → 从当前块切出 [offset, min(块尾, range.end)) 这一段 &str
  offset += 段长
  若 offset 已到块尾 → 树游标 next() 进入下一块
循环直到 offset 越过 range.end（offset_is_valid 返回 false）→ 返回 None
```

反向迭代把 seek 点放在 `range.end`、每轮 `offset -= 段长`、块游标 `prev()`，完全对称。

#### 4.4.3 源码精读

入口方法是一组薄封装（[src/rope.rs:L336-L367](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L336-L367)）：`chars()` 委托给 `chars_at(0)`，而 `chars_at` 本质是 `chunks_in_range(start..len()).flat_map(str::chars)`——**逐块借用再逐块解码字符**，永远不会物化整串。

`Chunks` 的构造函数里藏着本讲反复强调的契约：

```rust
let chunk_offset = offset - chunks.start();
if let Some(chunk) = chunks.item() {
    chunk.assert_char_boundary::<true>(chunk_offset);
}
```

见 [src/rope.rs:L806-L825](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L806-L825)：`reversed` 时校验的是 `range.end`。`assert_char_boundary::<true>` 的 `PANIC = true` 表示非法输入直接 panic 而不是记日志。

`Iterator::next` 的实现（[src/rope.rs:L1086-L1105](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1086-L1105)）就是流程图的两行翻译：`peek()` 拿段、`offset += chunk.len()`、到块尾就 `self.chunks.next()`。

`Bytes` 更朴素：每次 `next()` 交出一整块的 `&[u8]`（[src/rope.rs:L1143-L1157](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1143-L1157)）；它还实现了 `io::Read`（[src/rope.rs:L1159-L1184](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1159-L1184)），所以可以直接喂给任何接收 `Read` 的 API（比如写文件、哈希）。

`Display` 与 `Debug`（[src/rope.rs:L653-L676](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L653-L676)）都是逐块输出：

```rust
impl fmt::Display for Rope {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for chunk in self.chunks() {
            write!(f, "{}", chunk)?;
        }
        Ok(())
    }
}
```

`Display` 逐块 `write!`（因此 `rope.to_string()` 可用，但会分配一份完整副本）；`Debug` 额外处理引号和转义，用一个小的 `format_string` 缓冲逐块复用，避免为调试输出再建一份大字符串。另注意：测试代码里的 `rope.text()`（[src/rope.rs:L2509-L2517](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L2509-L2517)）是 `mod tests` 里的私有扩展方法，**不是**公开 API。

最后看一个现成的行为测试，学习「怎么用断言描述迭代器语义」（[src/rope.rs:L1841-L1855](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1841-L1855)）：

```rust
let rope = Rope::from("abc\ndefg\nhi");
let mut lines = rope.chunks().lines();
assert_eq!(lines.next(), Some("abc"));
assert_eq!(lines.next(), Some("defg"));
assert_eq!(lines.next(), Some("hi"));
assert_eq!(lines.next(), None);
```

`Chunks::lines()`（[src/rope.rs:L1017-L1025](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1017-L1025)）把块迭代器包成 `Lines`，跨块的行会被拼进一个复用的 `String`（[src/rope.rs:L1194-L1242](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L1194-L1242)）。同一测试还覆盖了 `"abc\ndefg\nhi\n"` 末尾空行的情形（返回四个 `Some`，最后一个是 `Some("")`）。

#### 4.4.4 代码实践

1. **实践目标**：观察块的真实大小，验证「块切点落在字符边界」。
2. **操作步骤**（示例代码）：

   ```rust
   use rope::Rope;

   fn main() {
       // 重复一段中英混排文本，制造多个块
       let text = "中文abc🦀".repeat(40); // 每段 13 字节（6+3+4），共 520 字节
       let rope = Rope::from(text.as_str());
       for (i, chunk) in rope.chunks().enumerate() {
           println!("chunk {}: {} bytes, 尾字节可打印 = {:?}",
                    i, chunk.len(), chunk.chars().last());
       }
       println!("总块数近似 = {}", rope.chunks().count());
   }
   ```

3. **需要观察的现象**：每块长度 ≤ 128 且各不相同；块边界处的字符没有被拆坏（`chunk.chars().last()` 总是完整字符）。
4. **预期结果**：520 字节超过 `push_large` 阈值（496），约切成 5 块（每块约 125～126 字节，末块为余量），每块 `len()` ≤ 128；如果把各块依次拼接，应与原文本完全一致（可用 `format!("{}", rope)` 对照）。精确块数依赖切点回退位置，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`chunks()` 和 `chars()` 的 `Item` 类型分别是什么？各自适合什么场景？

**答案**：`chunks()` 产出 `&'a str`（零拷贝借用块），适合能用 `str` 方法（`find`、`split`、`trim`）整段处理的场景；`chars()` 产出 `char`，适合状态机式逐字符处理。`chars()` 本身就是 `chunks().flat_map(str::chars)`（[src/rope.rs:L340-L342](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L340-L342)）。

**练习 2**：`rope.chunks_in_range(3..9)` 在什么输入下会 panic？

**答案**：当 3（正向迭代时校验的是 `range.start`）不落在字符边界时。构造函数会执行 `chunk.assert_char_boundary::<true>(chunk_offset)`（[src/rope.rs:L816-L818](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L816-L818)）。例如文本以 `'🦀'` 开头时，`1..9`、`2..9`、`3..9` 都会 panic，`0..9` 或 `4..9` 不会。

**练习 3**：为什么 `Rope` 不提供 `as_str(&self) -> &str`？

**答案**：`&str` 要求内存连续，而 rope 的文本分散在多个 `Chunk`（每个 ≤128 字节）里；提供整串借用要么强制重新分配拼接（那和 `to_string()` 没区别），要么需要 rope 之外的新表示。所以 API 面向「分块消费」设计，`Display` / `to_string()` 承担「偶尔需要整串」的需求。

## 5. 综合实践

**任务：实现 `word_count(rope: &Rope) -> usize`，全程不调用 `to_string()`。**

这是本讲所有内容的合流：用 `From`/`push` 构建、用 `chunks()` 分块消费、用 `summary()` 校验、用 `replace` 制造测试用例。

### 步骤一：搭建独立小程序（如果你还没做）

```bash
cargo new rope-playground --lib
cd rope-playground
```

在 `Cargo.toml` 中加入 path 依赖（路径按你的实际仓库位置调整）：

```toml
[dependencies]
rope = { path = "../zed-industries-zed/crates/rope" }
```

> 注意：rope 的部分公开类型（如 `Bias`）来自 `sum_tree`，若练习中用到需一并加 `sum_tree = { path = "../zed-industries-zed/crates/sum_tree" }`（以 workspace 内实际路径为准）。

### 步骤二：实现函数（示例代码，写入 `src/lib.rs`）

```rust
use rope::Rope;

/// 统计空白分隔的“单词”数：连续的非空白字符算一个词。
/// 与 str::split_whitespace().count() 的口径一致。
pub fn word_count(rope: &Rope) -> usize {
    let mut count = 0;
    let mut in_word = false;
    for chunk in rope.chunks() {
        for c in chunk.chars() {
            let is_sep = c.is_whitespace();
            if !is_sep && !in_word {
                count += 1;
            }
            in_word = !is_sep;
        }
    }
    count
}
```

关键点：`in_word` 状态**跨块保持**。如果一个词恰好被块边界劈开（比如 `"hel"` 在第 3 块末尾、`"lo"` 在第 4 块开头），逐块独立统计会把它数成两个词——外层持有状态即可修复，这是所有「分块处理」代码的通用套路。

### 步骤三：写对拍测试（写入 `tests/word_count.rs`）

```rust
use rope::Rope;
use rope_playground::word_count;

fn reference(text: &str) -> usize {
    text.split_whitespace().count()
}

#[test]
fn ascii_and_multibyte() {
    let samples = [
        "",
        "hello world",
        "  leading and  trailing  ",
        "你好 world",
        "Rust 🦀 语言 test",
        "a\nb\nc\n",
    ];
    for text in samples {
        let rope = Rope::from(text);
        assert_eq!(word_count(&rope), reference(text), "text: {:?}", text);
        assert_eq!(rope.len(), text.len()); // len 是字节数
        assert_eq!(rope.summary().chars, text.chars().count());
    }
}

#[test]
fn after_replace() {
    let mut rope = Rope::from("one two three");
    rope.replace(4..7, "TWENTY"); // "two" -> "TWENTY"
    assert_eq!(word_count(&rope), 3);
    assert_eq!(rope.summary().chars, 16); // "one TWENTY three"
}

#[test]
fn long_text_spans_many_chunks() {
    // 足够长以触发 push_large 与多块（生产阈值 496 字节）
    let text = "word ".repeat(10_000);
    let rope = Rope::from(text.as_str());
    assert_eq!(word_count(&rope), 10_000);
    assert_eq!(rope.summary().len, text.len());
}
```

### 步骤四：运行并观察

```bash
cargo test
```

**预期结果**：

- `"hello world"` → 2；`"你好 world"` → 2（`"你好"` 中间无空白，算一个词）；`"Rust 🦀 语言 test"` → 4。
- `after_replace` 中 `"one TWENTY three"` 共 16 个字符、3 个词。
- 长文本测试确保块边界劈词时 `in_word` 逻辑正确。
- 对照版本 `reference` 直接用 `split_whitespace`，任何不一致都说明你的状态机有 bug。

完整运行输出待本地验证；若编译报错，优先检查 path 依赖路径和 crate 名（`rope-playground` 的包名会连字符转下划线成为 `rope_playground`）。

### 步骤五（可选进阶）

把 `word_count` 改成只统计某个区间（加参数 `range: Range<usize>`，用 `rope.chunks_in_range(range)`），并思考：如果区间的起点把一个词劈成两半，返回值应该怎么定义？这正是块边界问题在 API 层的投影。

## 6. 本讲小结

- 构建 `Rope` 的所有入口（`From<&str>`、`push`、`push_front`、`append`）最终都走「填满尾块 → 按字符边界切 ≤128 字节的新块 → 挂上 `SumTree`」这条路径，超过约 496 字节的写入转投 `push_large`（可 rayon 并行）。
- `replace`/`slice`/`slice_rows` 的统一心法是「Cursor 拆成前缀 + 后缀，中间换掉再拼回」，中间完整块在树层面共享，不拷贝文本。
- **byte offset 是基准坐标**：`len()` 返回 UTF-8 字节数，所有区间参数都是字节区间，且端点必须落在字符边界，否则 panic；防御手段是 `is_char_boundary` / `clip_offset`。
- `len` / `summary` / `max_point` / `is_empty` 都是 \( O(1) \)，因为它们只是转发树根缓存的摘要。
- 消费文本的唯一正统姿势是迭代器：`chunks()`（零拷贝 `&str`）、`chars()`（逐字符）、`bytes_in_range()`（字节 / `io::Read`）；`Rope` 故意不提供 `as_str()`。
- `Display` 逐块输出（`to_string()` 可用但要付整串分配的代价）；`Debug` 输出带引号的转义形式；测试里的 `text()` 是私有扩展，不要当公开 API 记。

## 7. 下一步学习建议

本讲刻意回避了三个方向，它们正好是单元二的内容：

1. **u2-l1（坐标系统）**：本讲的 `Point` 只在 `max_point()` 露了一面。下一讲完整拆解 `Point` / `PointUtf16` / `OffsetUtf16` / `Unclipped` 四套坐标及其 `Add`/`Sub` 语义——特别是「`Point` 加法为什么不可交换」。
2. **u2-l2（TextSummary）**：本讲只用过 `len`、`chars`、`lines` 三个字段，还有 `first_line_chars`、`longest_row` 等六个字段没讲，它们的合并代数（`AddAssign`）是理解一切 \( O(1) \) 查询的基础。
3. **u2-l7（遍历与读取）**：本讲把 `Chunks` 当黑盒迭代器用；下一讲深入 `Cursor::seek_forward` / `slice` / `summary` / `suffix` 的可复用游标模型，以及 `next_line` / `prev_line` 的行定位。

建议在进入下一讲前，先完成本讲综合实践并重读 [src/rope.rs:L147-L202](https://github.com/zed-industries/zed/blob/00c0e96e769062e373203c62830f510fa121db76/crates/rope/src/rope.rs#L147-L202)（`push`）直到能独立复述两段式流程——它是后续所有写入路径的地基。
