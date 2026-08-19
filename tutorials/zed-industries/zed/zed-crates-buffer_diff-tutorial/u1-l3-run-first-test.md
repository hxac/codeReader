# 把项目跑起来:cargo test 与第一个测试

## 1. 本讲目标

学完本讲,你应该能够:

1. 用 `cargo test -p buffer_diff` 运行整个 crate 的测试,并会用过滤参数只运行单个测试。
2. 解释 `#[gpui::test]` 与 `gpui::TestAppContext` 在测试里分别扮演什么角色:为什么测试函数是 `async` 的、`cx` 从哪里来。
3. 读懂 `BufferDiffSnapshot::new_sync` 这个测试专用辅助函数:它是如何绕过异步流程、同步拿到一份「已经计算好 hunk」的快照的。
4. 会读会写 `assert_hunks` 的四元组断言:行区间、删除文本、新增文本、状态。
5. 能仿照现有测试新增一个属于自己的测试,并通过「故意改错断言」观察 `pretty_assertions` 的失败输出。

## 2. 前置知识

在动手之前,先 用通俗语言 对齐几个概念:

- **单元测试与 `cargo test`**:Rust 的单元测试直接写在源码文件里,放在 `#[cfg(test)] mod tests` 模块中。`#[cfg(test)]` 表示这个模块只在运行 `cargo test`(或 `cargo build --tests`)时才参与编译,正常的 `cargo build` 完全看不到它。`cargo test` 后面跟一个字符串时,是「按名字子串过滤」:只运行名字里包含该子串的测试。
- **workspace 与 `-p` 参数**:buffer_diff 不是独立项目,而是 zed 仓库这个巨大 Cargo workspace 中的一个成员。直接在仓库根目录运行 `cargo test` 会编译并运行所有 crate 的测试,非常慢;`cargo test -p buffer_diff` 表示只针对 `buffer_diff` 这一个包。
- **`TestAppContext`**:gpui 框架在测试环境里提供的「假应用上下文」。第一讲说过,`BufferDiff` 是一个 gpui 实体(`Entity<BufferDiff>`),创建实体需要一个 `App` 上下文;测试里没有真正的窗口和主循环,`TestAppContext` 就充当这个上下文,它内部带有一个测试专用的任务调度器,能让异步代码在测试中「原地跑完」。
- **`pretty_assertions`**:一个增强版断言库。标准库的 `assert_eq!` 失败时只打印两个值的 Debug 输出;`pretty_assertions::assert_eq!` 失败时会打印一个红绿高亮、类似 git diff 的对比视图,一眼就能看出两个值哪里不一样。它是本 crate 的正式依赖(不只是测试依赖),`assert_hunks` 内部就用了它。
- **`unindent`**:测试里写多行字符串时,为了代码美观会缩进每一行;`unindent()` 方法(来自 `unindent` crate 的 `Unindent` trait)负责去掉公共缩进和开头的空行,让字符串变成真正的文件内容。

如果你还没读过前两讲,至少需要知道:`BufferDiff` 是实体(可变)、`BufferDiffSnapshot` 是不可变快照、`DiffHunk` 描述一块差异、`DiffBaseKind::Head` 表示以 git HEAD 为基准。

## 3. 本讲源码地图

本讲全部源码集中在 crate 的唯一源码文件及其 `Cargo.toml` 中:

| 文件 | 区段 | 作用 |
| --- | --- | --- |
| `crates/buffer_diff/Cargo.toml` | `[dev-dependencies]` | 测试专用依赖:ctor、gpui(test-support)、rand、settings、text(test-support)、unindent、zlog |
| `crates/buffer_diff/src/buffer_diff.rs` | L86-L113 | `DiffHunkStatus` / `DiffHunkStatusKind` / `DiffHunkSecondaryStatus` 定义 |
| 同上 | L275-L284 | `BufferDiffSnapshot::new_sync`,本讲的测试辅助函数 |
| 同上 | L323-L338、L430-L438 | `hunks_intersecting_range` 与 `hunks()` 查询入口(测试里用来取出全部 hunk) |
| 同上 | L1651-L1675 | `BufferDiff::new_with_base_text`,`new_sync` 内部调用的构造函数 |
| 同上 | L2164-L2175 | `BufferDiff::snapshot()`,从实体取出快照 |
| 同上 | L2363-L2382 | `DiffHunkStatus::deleted_none` / `added_none` / `modified_none` 构造器 |
| 同上 | L2385-L2423 | `assert_hunks`,四元组断言工具 |
| 同上 | L2425-L2440 | `mod tests` 的模块头:导入与日志初始化 |
| 同上 | L2442-L2496 | `test_buffer_diff_simple`,本讲精读的第一个测试 |
| `crates/text/src/text.rs` | L1907-L1911 | `impl Deref for Buffer`,解释测试里为何能把 `&Buffer` 当 `&BufferSnapshot` 用 |
| `crates/text/src/anchor.rs` | L87-L89 | `Anchor::min_max_range_for_buffer`,测试里构造「全 buffer 范围」的锚点 |

## 4. 核心概念与源码讲解

### 4.1 测试模块的组织:`#[cfg(test)] mod tests` 与 `#[gpui::test]`

#### 4.1.1 概念说明

buffer_diff 的全部实现都在一个约 4300 行的文件里,测试也全部堆在这个文件的底部,用一个 `#[cfg(test)] mod tests` 模块包起来。这种「实现 + 测试同文件」的组织方式意味着:

- 测试通过 `use super::*` 可以直接访问 crate 内部的**私有**项(比如下一节的 `new_sync` 就不是 `pub` 的),所以测试能覆盖内部实现细节,而不只是公开 API。
- 想找测试,直接在文件里搜索 `mod tests` 即可,不用切换目录。

`#[gpui::test]` 是 gpui 提供的测试属性宏,标注在 `async fn` 上。它做两件事:

1. 为这次测试初始化一个 gpui 测试运行环境(测试调度器 + 假 `App`),等价于把整个应用的「心脏」换成一个可以在测试进程里原地跳动的简化版。
2. 把 `async fn` 的 future 交给这个测试调度器执行,并把 `&mut gpui::TestAppContext` 作为参数传进来——这就是测试签名里 `cx` 的来源。

因为有了 `TestAppContext`,测试里才能使用 `cx.new(...)` 创建实体、让后台计算在测试中同步完成,而无需启动真正的 Zed 应用。

#### 4.1.2 核心流程

一个典型测试的骨架是:

```text
1. 准备文本:用 "...".unindent() 写出带缩进的多行字符串,得到规整的 base/buffer 文本
2. 构造 buffer:text::Buffer::new(ReplicaId::LOCAL, BufferId, buffer_text)
3. 构造 diff:BufferDiffSnapshot::new_sync(&buffer, diff_base, cx)   ← 同步算好 hunk
4. 断言:assert_hunks(diff.hunks_intersecting_range(...), &buffer, &diff_base, &期望四元组)
5. (可选)修改 buffer,重复 3-4,验证增量行为
```

#### 4.1.3 源码精读

先看模块头和导入:

[crates/buffer_diff/src/buffer_diff.rs:2425-2440](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2425-L2440)

这段代码定义了 `mod tests`:`use super::*` 引入父模块(即整个 crate)的所有项;随后显式导入 `gpui::TestAppContext`、`pretty_assertions` 的断言宏、`rand` 的随机数生成器(供后面的随机化测试用)、`text` 的 `Buffer`/`BufferId`/`ReplicaId`、`unindent::Unindent` 以及 `util::test::marked_text_ranges`(一个用特殊标记圈出文本区间的辅助工具,后面的测试会用到)。`#[ctor::ctor(unsafe)] fn init_logger()` 借助 ctor 库注册了一个「在测试进程启动时自动执行」的函数,调用 `zlog::init_test()` 初始化日志系统——这样被测代码内部发出的日志警告才会在测试输出里可见。

测试专用依赖集中在 `Cargo.toml` 的 `[dev-dependencies]` 段:

[crates/buffer_diff/Cargo.toml:32-39](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/Cargo.toml#L32-L39)

注意两个细节:`gpui` 在这里额外打开了 `test-support` feature(提供 `TestAppContext` 等测试设施);`settings` 也在测试依赖里——它平时是可选 feature(见 `[features] test-support = ["settings"]`),测试时无条件启用,因为 word diff 等行为要读语言设置。

再看第一个测试 `test_buffer_diff_simple` 的前半部分:

[crates/buffer_diff/src/buffer_diff.rs:2442-2468](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2442-L2468)

逐行拆解:

- `#[gpui::test] async fn test_buffer_diff_simple(cx: &mut gpui::TestAppContext)`:gpui 风格的异步测试,`cx` 就是测试上下文。
- `let diff_base = "...".unindent();`:得到 `"one\ntwo\nthree\n"`(开头的空行和每行的公共缩进都被去掉,但保留行尾换行)。
- `Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text)`:创建一个本地副本、编号为 1 的文本 buffer,内容是 `"one\nHELLO\nthree\n"`。
- `BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx)`:同步计算 diff,返回快照(下一节精读)。
- `diff.hunks_intersecting_range(Anchor::min_max_range_for_buffer(buffer.remote_id()), &buffer)`:取出「与整个 buffer 相交」的所有 hunk。`Anchor::min_max_range_for_buffer` 定义在 [crates/text/src/anchor.rs:87-89](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/anchor.rs#L87-L89),它构造「该 buffer 的最开头到最结尾」这对锚点;由于任何 hunk 都落在这个范围里,效果就是「全部 hunk」。
  - 这里有个初学者容易疑惑的点:`hunks_intersecting_range` 的签名要求传 `&text::BufferSnapshot`([crates/buffer_diff/src/buffer_diff.rs:323-338](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L323-L338)),而 `buffer` 明明是 `text::Buffer`。答案是解引用强制转换:`text::Buffer` 实现了 `Deref<Target = BufferSnapshot>`(见 [crates/text/src/text.rs:1907-1911](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/text/src/text.rs#L1907-L1911)),所以 `&buffer` 会被自动当作 `&BufferSnapshot` 传入。事实上 crate 还提供了更直白的快捷方式 `hunks()`([crates/buffer_diff/src/buffer_diff.rs:430-438](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L430-L438)),它内部做的就是「min/max 锚点 + `hunks_intersecting_range`」这一套。
- 最后把迭代器交给 `assert_hunks`,期望只有一条 hunk:`(1..2, "two\n", "HELLO\n", modified_none())`——第 1 行的 `two` 被换成了 `HELLO`。四元组的确切含义在 4.3 节展开。

#### 4.1.4 代码实践

1. **实践目标**:亲手把 `test_buffer_diff_simple` 跑起来,确认本机环境可用,并学会按名字过滤测试。
2. **操作步骤**:
   1. 在 zed 仓库根目录执行 `cargo test -p buffer_diff test_buffer_diff_simple`。
   2. 再执行 `cargo test -p buffer_diff -- --list`,浏览这个 crate 一共有哪些测试(`--` 之后的参数会原样传给测试可执行文件,`--list` 是 libtest 的标准参数)。
3. **需要观察的现象**:第一步输出中出现 `test_buffer_diff_simple ... ok`,以及最后的 `test result: ok` 汇总行;第二步输出一个测试名列表。
4. **预期结果**:测试通过(该测试在 CI 中持续运行,HEAD 提交下应当是绿的)。首次运行会编译较多依赖,耗时较长属正常现象。汇总行里「共多少个测试通过」的具体数字,待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:如果删掉 `#[gpui::test]` 上的属性、把函数改成普通 `async fn`,测试还能跑吗?为什么?

**答案**:不能正常跑。Rust 原生测试执行器([test] / cargo test)不会驱动任意 future;`#[gpui::test]` 的职责之一就是把异步测试体挂到 gpui 的测试调度器上执行,并同时构造 `TestAppContext`。没有它,`cx` 参数无从产生,函数也无法被 libtest 正确执行。

**练习 2**:为什么 `mod tests` 里的代码不会让正式构建(`cargo build -p buffer_diff`)变慢或引入测试依赖?

**答案**:模块标注了 `#[cfg(test)]`,在非测试构建下整个模块根本不参与编译;`[dev-dependencies]` 里的 crate 也只在编译测试/示例/benchmark 时才会被拉进来。

### 4.2 `BufferDiffSnapshot::new_sync`:同步拿到计算好的快照

#### 4.2.1 概念说明

真实运行时的 diff 计算是异步的:`BufferDiff` 实体创建后,基准文本由 project 模块异步灌注,计算放到后台线程,算完再回写实体并广播事件。测试如果照搬这套流程,每个用例都要写一堆 `await` 和事件等待,非常啰嗦。

`new_sync` 就是测试专用的「作弊通道」:给定 buffer 快照和一段基准文本,**一步**得到已经算好 hunk 的 `BufferDiffSnapshot`。它有两个关键设计:

- 标注 `#[cfg(test)]`,只在单元测试中存在——注意它连 `pub` 都不是,所以只有本 crate 的 `mod tests`(通过 `use super::*`)能用它;而它内部调用的 `new_with_base_text` 标注的是 `#[cfg(any(test, feature = "test-support"))]`,下游 crate 打开 `test-support` feature 后可以使用后者,但不能使用 `new_sync`。
- 名字里的 `sync` 不是「又搞了一套同步算法」,而是「用 `block_on` 把异步计算堵在当前线程上等结果」。

#### 4.2.2 核心流程

`new_sync` 的执行过程:

```text
new_sync(buffer, diff_base, cx)
 ├─ cx.new(|cx| BufferDiff::new_with_base_text(&diff_base, buffer, cx))
 │   ├─ BufferDiff::new(buffer, None, None, DiffBaseKind::Head, cx)   // 先建空实体
 │   ├─ text::LineEnding::normalize(&mut base_text)                   // CRLF 归一为 LF
 │   ├─ cx.new(...) 建一个只读 language::Buffer 存基准文本
 │   ├─ block_on(this.update_diff(...))  // 前台执行器上阻塞等 diff 算完
 │   └─ this.set_snapshot(update, cx)    // 结果写回实体
 └─ buffer_diff.update(cx, |diff, cx| diff.snapshot(cx))              // 取出快照返回
```

#### 4.2.3 源码精读

先看 `new_sync` 本体:

[crates/buffer_diff/src/buffer_diff.rs:275-284](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L275-L284)

这段代码先用 `cx.new` 创建 `BufferDiff` 实体(构造逻辑全部在 `new_with_base_text` 里),再用 `buffer_diff.update(...)` 进入实体、调用 `snapshot(cx)` 把算好的快照克隆出来返回。注意参数 `buffer: &text::BufferSnapshot` 接收的是 buffer 的**快照**——正如 4.1 提到的,测试里传 `&buffer`(`&text::Buffer`)会经解引用自动转换。

再看它调用的构造函数:

[crates/buffer_diff/src/buffer_diff.rs:1651-1675](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1651-L1675)

这段代码做了五件事,对应上面流程图的五个分支:

1. 以 `DiffBaseKind::Head` 建一个普通的空 `BufferDiff`;
2. `text::LineEnding::normalize(&mut base_text)` 把基准文本的行尾统一成 `\n`——这就是为什么即使测试机器上行尾混乱,diff 结果仍然稳定(第五讲会专门讲 CRLF 测试);
3. 建一个 `Capability::ReadOnly` 的 `language::Buffer` 来持有基准文本,这也印证了第一讲说的「基准文本本身也是一个只读 buffer」;
4. `cx.foreground_executor().block_on(this.update_diff(...))`:在**前台**执行器上阻塞等待 diff 计算完成——测试调度器里没有别的线程竞争,这样做是安全的,也正是「sync」的来源;
5. `set_snapshot(update, cx)` 把计算结果写进实体。

最后看取快照的一端:

[crates/buffer_diff/src/buffer_diff.rs:2164-2175](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2164-L2175)

这段代码是 `BufferDiff::snapshot()`:如果实体已有一份计算结果(`diff_snapshot` 为 `Some`)就直接克隆返回;否则兜底构造一个「hunk 树为空、`base_text_exists = false`」的空快照——第二讲已经见过这个兜底分支。`new_with_base_text` 末尾的 `set_snapshot` 保证了 `new_sync` 拿到的一定是前者,即真正算好的那份。`test_buffer_diff_simple` 的第三段断言正是利用了兜底分支:`BufferDiff::new(&buffer, None, None, DiffBaseKind::Head, cx)` 不给基准文本直接建实体,`snapshot()` 落入 `else` 分支,于是没有任何 hunk:

[crates/buffer_diff/src/buffer_diff.rs:2485-2495](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2485-L2495)

(这段用 `cx.update` 在测试上下文里直接操作 `App`,期望 hunk 列表为空。)

#### 4.2.4 代码实践

1. **实践目标**:用 `new_sync` 构造 diff,观察快照上几个「只看字段就能懂」的查询方法,体会「一次调用、同步拿到结果」。
2. **操作步骤**:
   1. 在 `mod tests` 中(比如紧挨 `test_buffer_diff_simple` 之后)临时新增下面的测试,运行 `cargo test -p buffer_diff test_observe_new_sync`。

   以下为示例代码(仿照 `test_buffer_diff_simple` 编写,非项目原有代码):

   ```rust
   #[gpui::test]
   async fn test_observe_new_sync(cx: &mut gpui::TestAppContext) {
       let diff_base = "a\nb\nc\n".to_string();
       let buffer_text = "a\nB\nc\n".to_string();

       let mut buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
       let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);

       assert!(diff.base_text_exists());
       assert!(!diff.is_empty());
       assert_eq!(diff.base_text_string().as_deref(), Some("a\nb\nc\n"));
       assert_eq!(diff.changed_row_counts(), (1, 1)); // (新增行数, 删除行数)

       buffer.edit([(0..0, "point five\n")]); // 在文件开头插入一行
       let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base, cx);
       assert_eq!(diff.changed_row_counts(), (2, 1));
   }
   ```

   2. 观察断言通过后,把最后一条断言改成 `(1, 1)` 再运行,看失败信息。
   3. 实践结束后用 `git checkout -- crates/buffer_diff/src/buffer_diff.rs` 还原源码。
3. **需要观察的现象**:两次 `new_sync` 之间只插入了一行,`changed_row_counts` 从 `(1, 1)` 变为 `(2, 1)`。
4. **预期结果**:第一条 diff 只有一个「b → B」的修改 hunk:buffer 侧占 1 行、base 侧占 1 行,故为 `(1, 1)`;插入行后多出一个纯新增 hunk(buffer 侧 1 行、base 侧 0 行),合计 `(2, 1)`。这一计数规则可以在 [crates/buffer_diff/src/buffer_diff.rs:186-199](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L186-L199) 中直接验证:`added_rows` 是 hunk 的 buffer 行跨度、`removed_rows` 是 base 行跨度。本示例代码未在撰写环境中运行,待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:`new_sync` 标注的是 `#[cfg(test)]`,而 `new_with_base_text` 标注的是 `#[cfg(any(test, feature = "test-support"))]`。这个差异意味着什么?

**答案**:`new_sync` 只在本 crate 的单元测试中可用(它不是 `pub`,配合 `use super::*` 使用);`new_with_base_text` 还对打开了 `test-support` feature 的下游 crate 开放(`pub`),供 editor、project 等 crate 的集成测试构造带基准文本的 diff。

**练习 2**:为什么 `new_with_base_text` 里要先 `LineEnding::normalize`?

**答案**:把基准文本的行尾(CRLF 等)统一归一成 LF,保证 diff 计算和后续断言不被平台行尾差异干扰;真实链路中 buffer 侧同样有行尾归一化处理(第五讲的 CRLF 测试会覆盖这一点)。

### 4.3 `assert_hunks`:四元组断言工具

#### 4.3.1 概念说明

一个 `DiffHunk` 有锚点区间、字节区间、词级差异、状态等一大堆字段,直接断言非常繁琐。`assert_hunks` 把「人真正关心的信息」提炼成四元组:

| 位置 | 字段 | 含义 | 取值来源 |
| --- | --- | --- | --- |
| 1 | 行区间(buffer 侧行号) | hunk 覆盖 buffer 的第几行到第几行 | `hunk.range`(Point 区间)的行部分 |
| 2 | 删除文本 | 这次改动从基准文本里删掉了什么 | 用 `diff_base_byte_range` 去切片基准字符串 |
| 3 | 新增文本 | buffer 里现在是什么内容 | 用 `hunk.range` 从 buffer 取文本 |
| 4 | 状态 | Added / Modified / Deleted(+staged 子状态) | `hunk.status()` |

期望值侧只写行号(`Range<u32>`),断言内部会放大成 `Point::new(start, 0)..Point::new(end, 0)`;文本参数是泛型 `AsRef<str>`,所以 `"two\n"` 和 `String` 都能传。

#### 4.3.2 核心流程

```text
assert_hunks(实际迭代器, buffer, diff_base, 期望列表)
 ├─ 把每个实际 hunk 映射为 (行区间, base切片文本, buffer文本, status)
 ├─ 把每个期望四元组映射为 (Point区间, 删除文本, 新增文本, status)
 └─ pretty_assertions::assert_eq!(两个 Vec)   ← 失败时输出红绿 diff
```

#### 4.3.3 源码精读

[crates/buffer_diff/src/buffer_diff.rs:2385-2423](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2385-L2423)

这段代码是 `assert_hunks` 的全部实现,几个值得注意的设计:

- `#[track_caller]`(L2386):让 panic 的位置定位到**调用处**(即具体测试的那一行),而不是断言函数内部——失败信息直接指向你写的期望,而不是工具函数。
- `#[cfg(any(test, feature = "test-support"))]`(L2385):和 `new_with_base_text` 一样,对下游的集成测试开放,所以 editor 等 crate 的测试也在用它。
- 「删除文本」来自 `&diff_base[hunk.diff_base_byte_range.clone()]`(L2401):用 hunk 记录的基准侧字节区间去切片你传入的 `diff_base` 字符串——这也是调用时必须把**原始基准文本**一并传进来的原因。
- 「新增文本」来自 `buffer.text_for_range(hunk.range.clone())`(L2402-2404):从 buffer 快照按行区间取文本。
- 最终比较交给 `pretty_assertions::assert_eq!`(L2422):两个 `Vec` 逐元素比较,失败时以红绿高亮 diff 呈现差异。

期望中的状态值用到三个快捷构造器:

[crates/buffer_diff/src/buffer_diff.rs:2363-2382](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2363-L2382)

这段代码定义 `deleted_none()` / `added_none()` / `modified_none()`:它们把 `kind` 设为对应种类,并把 `secondary` 固定为 `NoSecondaryHunk`。`DiffHunkStatus` 本身是 `kind + secondary` 两个字段的结构体([crates/buffer_diff/src/buffer_diff.rs:86-97](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L86-L97));`secondary` 描述「这一块相对 git index 是否已暂存」(取值见 [crates/buffer_diff/src/buffer_diff.rs:99-113](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L99-L113)),本讲的测试没有挂 secondary diff,所以一律用 `_none` 后缀的构造器。`kind` 的判定规则(第二讲讲过,此处复习):buffer 侧区间空 → Deleted;base 侧区间空 → Added;两侧都非空 → Modified。

最后回看 `test_buffer_diff_simple` 中「一次编辑后变两条 hunk」的断言,验证你已能读懂四元组:

[crates/buffer_diff/src/buffer_diff.rs:2470-2483](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2470-L2483)

这段代码在 buffer 开头插入 `point five\n` 后重新计算 diff,期望两条 hunk:`(0..1, "", "point five\n", added_none())` 是纯新增(base 侧没有对应文本,所以删除文本为空串),`(2..3, "two\n", "HELLO\n", modified_none())` 是原有的修改,且因开头插入了一行,行号从 `1..2` 平移到了 `2..3`。注意重新计算调用的是 `BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx)`——对新的 buffer 内容重新跑一遍完整 diff,而不是增量更新;增量路径(`set_base_text` / `compare_hunks`)是第三讲的主题。

#### 4.3.4 代码实践

1. **实践目标**:亲手写一个产生**两个** hunk 的测试,并用 `assert_hunks` 断言删除文本、新增文本与状态;再故意改错一处,观察 `pretty_assertions` 的失败输出长什么样。
2. **操作步骤**:
   1. 在 `mod tests` 中 `test_buffer_diff_simple` 之后新增下面的测试。以下为示例代码(非项目原有代码):

   ```rust
   #[gpui::test]
   async fn test_buffer_diff_two_hunks(cx: &mut gpui::TestAppContext) {
       let diff_base = "
           one
           two
           three
           four
           five
       "
       .unindent();

       let buffer_text = "
           one
           TWO
           three
           four
           FIVE
       "
       .unindent();

       let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), buffer_text);
       let diff = BufferDiffSnapshot::new_sync(&buffer, diff_base.clone(), cx);
       assert_hunks(
           diff.hunks_intersecting_range(
               Anchor::min_max_range_for_buffer(buffer.remote_id()),
               &buffer,
           ),
           &buffer,
           &diff_base,
           &[
               (1..2, "two\n", "TWO\n", DiffHunkStatus::modified_none()),
               (4..5, "five\n", "FIVE\n", DiffHunkStatus::modified_none()),
           ],
       );
   }
   ```

   2. 运行 `cargo test -p buffer_diff test_buffer_diff_two_hunks`,应当通过。
   3. 把第一条期望的删除文本 `"two\n"` 故意改成 `"TOO\n"`(或把行区间 `1..2` 改成 `1..3`),再次运行,阅读失败输出;随后改回正确值,并用 `git diff` / `git checkout --` 管理好这次临时改动。
3. **需要观察的现象**:第 2 步测试通过;第 3 步失败输出中,`pretty_assertions` 以高亮 diff 的形式展示「实际四元组列表」与「期望四元组列表」的差异,并指出第一个不相等的元素;`#[track_caller]` 让 panic 位置指向你调用 `assert_hunks` 的行而不是工具函数内部。
4. **预期结果**:两条 hunk 分别对应第 1 行(`two → TWO`)与第 4 行(`five → FIVE`)的修改;两处修改相距两行未超出 diff 上下文合并阈值,因此各自成块、互不合并(相邻删除+新增何时会合并进同一个 hunk,是第五讲测试导读的话题)。失败输出的具体渲染样式,待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:把示例测试中的 buffer 文本改为只有四行(删掉 `five` 那行,即 buffer 缺一行),四元组应该怎么写?

**答案**:这条差异变成纯删除,写成 `(4..4, "five\n", "", DiffHunkStatus::deleted_none())`——buffer 侧行区间为空区间 `4..4`(即 `Point 4,0 .. Point 4,0`,删除发生在该位置),删除文本是基准里的 `five\n`,新增文本为空串,状态为 Deleted。具体空区间的行号是否恰好落在 `4..4`,待本地验证后确认。

**练习 2**:为什么 `assert_hunks` 需要调用者把 `diff_base` 原文传进来,而不是从快照里自己取?

**答案**:工具函数需要用「你写期望时的那字符串」做切片,这样期望里的删除文本(如 `"two\n"`)和实际切片用的是同一份基准文本,断言才可比;同时保持函数签名简单(不必依赖快照内部字段的可访问性)。快照侧也有 `base_text_string()` 可以取基准文本,但注意 `new_with_base_text` 会做行尾归一化,极端情况下与传入原文可能有差异,传入原文做对照更贴近测试意图。

## 5. 综合实践

把本讲三个模块串成一个完整闭环——「运行 → 模仿 → 破坏 → 还原」:

1. 在 zed 仓库根目录运行 `cargo test -p buffer_diff test_buffer_diff_simple`,确认环境可用(4.1)。
2. 阅读 [crates/buffer_diff/src/buffer_diff.rs:2442-2496](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2442-L2496),确认你能对上每一步:`unindent` → `Buffer::new` → `new_sync` → `hunks_intersecting_range` → `assert_hunks`(4.1-4.3)。
3. 仿照它新增 `test_buffer_diff_two_hunks`:基准与 buffer 各改一处、相隔数行,形成两个 hunk,用 `assert_hunks` 断言两个四元组的删除文本、新增文本与 `modified_none()` 状态(4.3 的示例代码即参考答案)。
4. 运行通过后,故意把一条期望的删除文本改错一个字符,重新运行,观察 `pretty_assertions` 的高亮 diff 输出,确认 panic 定位在你调用 `assert_hunks` 的那一行。
5. (可选进阶)给测试加一条 `println!("{:?}", diff.changed_row_counts());`,用 `cargo test -p buffer_diff test_buffer_diff_two_hunks -- --nocapture` 运行,验证计数与你的手算一致(4.2)。
6. 全部完成后还原源码:`git checkout -- crates/buffer_diff/src/buffer_diff.rs`,保证仓库干净。

完成这个闭环后,你就掌握了在本 crate 中「读任意一个测试并验证自己的理解」的全部工具,后续每一讲的实践都以它为基础。

## 6. 本讲小结

- 测试全部位于 [buffer_diff.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2425-L2440) 底部的 `#[cfg(test)] mod tests`,通过 `use super::*` 直接访问私有项;`ctor` 注册的 `init_logger` 在测试前初始化日志。
- `#[gpui::test]` + `async fn` + `&mut gpui::TestAppContext` 是本 crate 测试的标准签名:gpui 提供假应用上下文与测试调度器,让实体创建与异步计算在测试中可行。
- `BufferDiffSnapshot::new_sync`([L275-284](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L275-L284))是测试作弊通道:内部经 `new_with_base_text`(行尾归一化 → 只读基准 buffer → `block_on(update_diff)` → `set_snapshot`)一次拿到算好的快照。
- `assert_hunks`([L2385-2423](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2385-L2423))用四元组断言 hunk:buffer 行区间、删除文本(切基准字符串)、新增文本(切 buffer)、状态;`#[track_caller]` 让失败定位到调用处。
- 状态构造器 `added_none()` / `deleted_none()` / `modified_none()` 表示「无 secondary 参与」的三种基础状态;`DiffHunkStatus = kind + secondary`。
- 运行方式:仓库根目录 `cargo test -p buffer_diff <测试名子串>` 只跑单个测试,`-- --list` 列出全部测试,`-- --nocapture` 查看 println 输出。

## 7. 下一步学习建议

- 下一讲(u2-l1)深入 `DiffHunk` 与 `DiffHunkStatus`:本讲只是把状态当「期望值」用,下一讲会讲 `status()` 如何由两个区间的空与非空推导出来,以及 `is_created_file` 的判定;建议先重读 [crates/buffer_diff/src/buffer_diff.rs:86-113](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L86-L113) 做预习。
- 想先见识「测试作为规格说明」的威力,可以浏览同模块里的 `test_buffer_diff_range`、`test_changed_ranges`、`test_set_base_text_with_crlf`,尝试只看测试名和断言猜行为,再反推实现;这些会在第五讲系统导读。
- 对「为什么 hunk 恰好这样切分」感兴趣的读者,可以提前翻看 `compute_hunks` 与 `postprocess_lines`(第三讲 u3-l1 的主题),本讲的四元组断言正是验证那套算法输出的尺子。
