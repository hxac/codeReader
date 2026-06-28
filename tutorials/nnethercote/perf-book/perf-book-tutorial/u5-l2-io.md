# I/O——锁与缓冲

## 1. 本讲目标

本讲精读 perf-book 的 [I/O](src/io.md) 章，并取 [Heap Allocations](src/heap-allocations.md) 章中「Reading Lines from a File」一节作为承接 u3-l2 的桥梁。

读完本讲，你应当能够：

- 说清 `print!`/`println!` 每次**锁定 stdout** 的开销来源，并能用手动 `stdout().lock()` 把「每行一锁」改成「整批一锁」。
- 理解 Rust 文件 I/O **默认不缓冲**，知道用 `BufReader`/`BufWriter` 维护内存缓冲、减少系统调用次数，并能解释「写缓冲好忘、读缓冲代码会变样」这一非对称性。
- 掌握读文件逐行时的**分配取舍**：`BufRead::lines()` 每行分配一个 `String`，而用 workhorse `String` + `read_line` + `clear()` 可把分配降到「至多几次、甚至一次」（承接 u3-l2）。
- 了解读入 `String` 会带来 **UTF-8 校验开销**，并知道用 `read_until` 或 `bstr`/`linereader` 等「按字节」方式跳过这层校验。
- 把「锁」和「缓冲」组合起来用在 stdout 上，理解这两者是**正交**的优化维度。

---

## 2. 前置知识

本讲承接一篇前置讲义，并复用两条贯穿全册的纪律：

- **u3-l2 Vec 的增长、集合复用与分配回归**：你已经熟悉「workhorse 集合 + `clear()` 复用容量」的模式——把一个集合声明在循环外、循环体内用完即 `clear()`（清空长度但保留容量），从而避免每轮重新分配。u3-l2 的结论里专门点了「按行读文件用 `read_line` 而非 `lines()`」。本讲 4.3 会把这条结论展开成完整代码，正是 workhorse 模式在 I/O 上的直接应用。
- **u2-l1 Benchmarking / u2-l2 Profiling**：贯穿全册的纪律是「写法改动只是候选优化，收益必须靠测量确认」。I/O 尤其如此——加锁、加缓冲到底快不快，强烈依赖负载（输出多少行、每行多大、是否真的有竞争），一律以基准测试为准。

两个直白的直觉：

1. **系统调用（syscall）很贵。** 一次 `write` 系统调用要从用户态陷入内核、由内核把字节搬到文件/终端/套接字。调用次数越多，这个「陷入」的固定开销累积越大。缓冲的本质就是「攒一大批、一次性交出去」，把 \( n \) 次小调用摊还（amortize）成少数几次大调用。
2. **锁也不是免费的。** 获取/释放一把锁涉及原子操作与可能的内核 futex 协调。`println!` 在「打印这一行」前后各做一次锁/解锁，循环里反复 `println!` 就是反复「开关门」。

> 术语对照：本讲的「缓冲（buffering）」指在用户态内存里积攒数据再批量交给内核；「锁（locking）」特指对 `stdout`/`stdin`/`stderr` 这三个标准流的并发访问锁，与一般互斥锁语义一致但作用域更窄。

---

## 3. 本讲源码地图

本讲涉及的「源码」就是 perf-book 的两个 Markdown 章节：

| 文件 | 作用 |
| --- | --- |
| [src/io.md](src/io.md) | 本章主体：Locking、Buffering、Reading Lines from a File、Reading Input as Raw Bytes 四节。 |
| [src/heap-allocations.md](src/heap-allocations.md) | 提供分配视角：其中的「Reading Lines from a File」一节是 io.md 同名小节的真正落点，讲 `lines()` vs `read_line` 的分配差异。 |

注意 perf-book 是一本用 mdBook 写的在线书，它的「源码」就是这些 Markdown 文稿（见 u1-l1）。本讲引用的代码块来自书稿本身；凡是为练习而新写的代码，都会明确标注为「示例代码」。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. 锁开销：`print!`/`println!` 的重复锁定与手动锁定 stdout/stdin。
2. 缓冲：`BufWriter`/`BufReader`（含读写两侧的非对称性与「锁+缓冲」组合）。
3. 逐行读文件的分配取舍：`lines()` 与 workhorse `String`（承接 u3-l2）。
4. 按字节读入原始输入：跳过 UTF-8 校验。

### 4.1 锁开销：`print!`/`println!` 与手动锁定 stdout/stdin

#### 4.1.1 概念说明

Rust 的 [`print!`] 和 [`println!`] 宏**每次调用都会锁定 stdout**。这不是宏本身的癖好，而是标准库为标准流提供的并发安全保证：多个线程同时往 stdout 写不会交错。代价是，每次 `println!` 都要在打印前获取锁、打印后释放锁。当你**反复**调用这些宏（典型场景：循环里逐行输出），「取锁—写—放锁」这套动作会重复成百上千万次，锁本身的固定开销就成了不可忽略的成本。

解决办法是**手动锁定**：在循环之前调用 `stdout().lock()` 拿到一个锁句柄 `StdoutLock`，然后对它反复写入；锁在 `StdoutLock` 被 drop 时才释放。这样整批输出共享**一次**加锁，把 \( n \) 次锁操作摊还成一次。

#### 4.1.2 核心流程

把两种写法并排看就很清楚：

```text
// 反复 println!：每行一次锁
for line in lines {
    println!("{}", line);   // 内部 ≈ lock(); 写一行; unlock();
}
// → n 行 = n 次 lock/unlock

// 手动锁定：整批一次锁
let lock = stdout().lock();
for line in lines {
    writeln!(lock, "{}", line)?;   // 复用同一把已持有的锁
}
// drop(lock) 时统一解锁
// → n 行 = 1 次 lock/unlock
```

要点：

- `writeln!(lock, ...)` 写到的是 `StdoutLock`，它实现了 `Write` trait，所以和写普通 `File` 的代码几乎一样。
- 锁的生命周期由 `lock` 这个绑定控制；它 drop 时自动解锁，**不需要**也**不应该**手动 unlock。
- 这只对**反复**操作有意义：单次 `println!` 再手动加锁纯属添麻烦。

#### 4.1.3 源码精读

书里对开销来源的描述见 [src/io.md:5-6](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L5-L6)：

> Rust's `print!` and `println!` macros lock stdout on every call. If you have repeated calls to these macros it may be better to lock stdout manually.

要改掉的「反面教材」见 [src/io.md:12-17](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L12-L17)——循环里逐行 `println!`：

```rust
for line in lines {
    println!("{}", line);
}
```

改写后见 [src/io.md:19-31](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L19-L31)——先取 stdout 再 `lock()`，循环里用 `writeln!`：

```rust
use std::io::Write;
let mut stdout = std::io::stdout();
let mut lock = stdout.lock();
for line in lines {
    writeln!(lock, "{}", line)?;
}
// stdout is unlocked when `lock` is dropped
```

书里紧接着补了一句重要延伸（[src/io.md:32](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L32)）：**stdin 和 stderr 也可以同样手动锁定**。也就是说，凡是对这三个标准流的「反复操作」，都适用同一招。

#### 4.1.4 代码实践

1. **实践目标**：亲手对比「循环 `println!`」与「手动锁定 + `writeln!`」的耗时差异。
2. **操作步骤**（示例代码）：
   ```rust
   // 示例代码：src/main.rs
   use std::io::{self, Write};

   fn main() -> io::Result<()> {
       let lines: Vec<String> = (0..100_000).map(|i| format!("line {i}")).collect();

       // 版本 A：每行一次锁
       // for line in &lines { println!("{}", line); }

       // 版本 B：整批一次锁
       let mut lock = io::stdout().lock();
       for line in &lines {
           writeln!(lock, "{}", line)?;
       }
       Ok(())
   }
   ```
   - 分别保留版本 A / B 编译，用 `cargo build --release`。
   - 用 Hyperfine（承接 u2-l1）对比，并务必把输出**重定向到文件**（`./target/release/<crate> > /dev/null` 或写文件），避免终端渲染本身成为瓶颈：
     ```bash
     hyperfine --warmup 3 './target/release/crate_a > /tmp/a.txt' \
                       './target/release/crate_b > /tmp/b.txt'
     ```
3. **需要观察的现象**：重定向到文件/`/dev/null` 时，版本 B 的 wall-time 应明显低于版本 A，差异随行数与并发度增大而放大。
4. **预期结果**：输出到文件时手动锁定更快；但**直接输出到终端**时两者可能几乎无差异（终端渲染是瓶颈）——这正好印证「收益依赖负载，须实测」。
5. **说明**：若手头无 Hyperfine，用 `time` 多次取最小值亦可；具体倍数待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：既然 `println!` 每次都锁，为什么标准库不干脆「锁一次永不释放」？

> **参考答案**：标准流是**全局共享**资源，多线程都可能写。永久持锁会让其他线程无法输出、甚至死锁。每次调用自动加解锁，是用「少量固定开销」换取「任何线程随时都能安全写入」的正确性保证。手动锁定只是把「这一批调用属于同一线程、可合并」的**额外信息**显式告诉运行时。

**练习 2**：手动锁定后忘了 drop `lock` 会怎样？

> **参考答案**：`StdoutLock` 在其绑定离开作用域时自动 drop 并解锁，所以正常代码不会「忘」。真正的风险是**作用域太大**——若把 `lock` 一直攥在手里跨过其他耗时操作，会不必要地阻塞其他线程写 stdout。建议把锁定范围限制在「真正需要批量输出」的最小区间。

---

### 4.2 缓冲：`BufReader`/`BufWriter`

#### 4.2.1 概念说明

Rust 的**文件 I/O 默认不缓冲**。这意味着对 `File` 的每一次 `write`（哪怕只写几个字节）都可能触发一次 `write` 系统调用，陷入内核、把这一点点数据交给操作系统。当你**高频、小块**地读写文件或网络套接字时，系统调用的固定开销会成为主导成本。

[`BufReader`] 和 [`BufWriter`] 解决的就是这个问题：它们在内存里维护一块缓冲区，把零碎的读写**攒起来**，等攒够（或显式 `flush`、或 drop）再一次性交给底层 reader/writer，从而**最小化系统调用次数**。

- **写**：`BufWriter` 把小写入暂存到内部缓冲，满了才一次性 `write` 到底层。
- **读**：`BufReader` 一次性从底层读一大块进缓冲，后续小块读取直接从缓冲里拿，命中就不再陷入内核。

#### 4.2.2 核心流程

写缓冲的「攒批」过程：

```text
小块写入 a,b,c,... ──▶ BufWriter 内部缓冲（攒）
                         │ 缓冲满 / 显式 flush() / drop
                         ▼
                    一次大 write 系统调用 ──▶ File / 套接字
```

读缓冲则反过来：一次大 `read` 把一大块搬进缓冲，后续读取从缓冲命中。

书里点出一条关键的**读写非对称性**，解释了「为什么忘加缓冲的情况，写比读更常见」：

| | 无缓冲器 | 有缓冲器 | 两者关系 |
| --- | --- | --- | --- |
| 写 | `File`（实现 `Write`） | `BufWriter`（也实现 `Write`） | **同一个 trait**，换一行构造代码即可，前后几乎一样 |
| 读 | `File`（实现 `Read`） | `BufReader`（实现 `BufRead`） | **不同 trait**（`Read` vs `BufRead`），读法本身就要改 |

正因为「写」两侧都是 `Write`，加不加 `BufWriter` 代码长得几乎一样，所以容易被**忘掉**却不出错；而「读」一旦想用 `BufRead::read_line`/`lines()` 这类方便方法，就**必须**包一层 `BufReader`，代码形态会变，反而很少被遗漏。

最后，缓冲**对 stdout 同样有效**：往 stdout 大量写入时，可以把「手动锁定」与「缓冲」**组合**起来——这两个优化是正交的，一个省锁、一个省系统调用。

#### 4.2.3 源码精读

书里对「默认不缓冲」与缓冲作用的说明见 [src/io.md:36-39](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L36-L39)：

> Rust file I/O is unbuffered by default. … use `BufReader` or `BufWriter`. They maintain an in-memory buffer for input and output, minimizing the number of system calls required.

反面教材（无缓冲写文件）见 [src/io.md:45-55](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L45-L55)，每次 `writeln!` 直接落到 `File`：

```rust
use std::io::Write;
let mut out = std::fs::File::create("test.txt")?;
for line in lines {
    writeln!(out, "{}", line)?;
}
```

改写后见 [src/io.md:57-68](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L57-L68)，用 `BufWriter` 包住 `File`，并在结尾 `flush()`：

```rust
use std::io::{BufWriter, Write};
let mut out = BufWriter::new(std::fs::File::create("test.txt")?);
for line in lines {
    writeln!(out, "{}", line)?;
}
out.flush()?;
```

书特别提醒了 `flush()` 的取舍（[src/io.md:72-75](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L72-L75)）：`BufWriter` 在 drop 时**会自动 flush**，所以显式 `flush()` 并非必需；但**自动 flush 时发生的错误会被忽略**，而显式 `flush()` 能让这个错误暴露出来（返回 `io::Result`）。也就是说，显式 flush 的价值不在于「让它刷新」（那一定会发生），而在于「**别把刷盘错误吞掉**」。

读写非对称性的完整论述见 [src/io.md:79-88](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L79-L88)（解释为什么书的「对照示例」只给了写、没给读）：因为读一侧要从 `Read` 切换到 `BufRead`，前后代码本来就不相似，做不出像写那样「换一行就够」的整洁对照。

「锁 + 缓冲」组合的提示见 [src/io.md:96-97](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L96-L97)：

> note that buffering also works with stdout, so you might want to combine manual locking *and* buffering when making many writes to stdout.

书还在 [src/io.md:69-70](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L69-L70) 给了两个真实 PR 示例链接（rust-lang/rust#93954、dhat-rs#22），可对照真实工程里加缓冲的收益。

#### 4.2.4 代码实践

1. **实践目标**：验证 `BufWriter` 对「高频小块写文件」的加速，并体会显式 `flush()` 暴露错误的价值。
2. **操作步骤**（示例代码）：
   ```rust
   // 示例代码
   use std::io::{BufWriter, Write};

   fn main() -> std::io::Result<()> {
       let lines: Vec<String> = (0..1_000_000).map(|i| format!("line {i}")).collect();

       // 版本 A：无缓冲，每行一次 write 系统调用
       // let mut out = std::fs::File::create("/tmp/unbuf.txt")?;

       // 版本 B：BufWriter 包一层
       let mut out = BufWriter::new(std::fs::File::create("/tmp/buf.txt")?);

       for line in &lines {
           writeln!(out, "{}", line)?;
       }
       out.flush()?;   // 显式 flush，让刷盘错误冒泡
       Ok(())
   }
   ```
   - 分别编译 A/B，用 Hyperfine 对比写满 100 万行的耗时。
   - （进阶）用 `strace -c` 统计两版的 `write` 系统调用次数。
3. **需要观察的现象**：版本 B 的 `write` 系统调用次数应从「约 100 万次」骤降到「少数几十次」，wall-time 显著下降。
4. **预期结果**：缓冲版本明显更快；`strace` 能直观看到系统调用数量级的差异。
5. **说明**：差异幅度取决于行数与每行大小，待本地验证；若环境无 `strace`，单看 Hyperfine 的耗时差即可。

#### 4.2.5 小练习与答案

**练习 1**：既然 `BufWriter` drop 时会自动 flush，为什么书里仍建议显式 `out.flush()?`？

> **参考答案**：自动 flush 一定会发生，但它发生在 `Drop::drop` 里，而 `drop` 不能返回错误，于是**刷盘时的 I/O 错误会被默默忽略**。显式 `flush()?` 把「把缓冲真正写到磁盘」这一步的 `io::Result` 暴露给调用方，让你能及时发现磁盘满、权限错等问题。

**练习 2**：「忘加缓冲」为什么在写一侧比读一侧更常见？

> **参考答案**：写一侧，`File` 和 `BufWriter` 都实现 `Write` trait，换构造方式即可、其余代码不变，所以容易忘却不出错；读一侧，要用 `BufRead` 的 `read_line`/`lines()` 等**便利方法**就必须显式包 `BufReader`，代码形态会改变，反而很难被遗漏。

---

### 4.3 逐行读文件的分配取舍：`lines()` 与 workhorse `String`（承接 u3-l2）

#### 4.3.1 概念说明

io.md 的「Reading Lines from a File」小节本身只有一句话，它把真正的讲法**委托**给了 heap-allocations.md 的同名小节（[src/io.md:99-105](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L99-L105)）。这里要讨论的是**读取这一步的堆分配**，而非系统调用。

最顺手的逐行读法是 `BufRead::lines()`，它返回一个迭代器，每 yield 一行就给你一个 `String`。问题是：它**每读一行就分配一个新的 `String`**。读一个百万行的文件，就是百万次堆分配。这正是 u3-l1 / u3-l2 反复强调的「分配率」问题在 I/O 上的具体化身。

解决办法是 u3-l2 学过的 **workhorse 模式**：循环外声明一个 `String`，循环内用 `read_line` 把内容读进它、处理完 `clear()` 清空（保留容量），下一轮复用同一块缓冲。这样整个文件的读取，分配次数被压到「**至多几次、甚至只有一次**」。

#### 4.3.2 核心流程

两种逐行读法的分配对比：

```text
lines() 路径：
  for line in lock.lines() {          // 每行 yield 一个新 String
      process(&line?);                // → 每行 1 次分配，n 行 = n 次
  }

workhorse String 路径：
  let mut line = String::new();       // 循环外声明一次
  while lock.read_line(&mut line)? != 0 {
      process(&line);                 // 复用同一块缓冲
      line.clear();                   // 清空长度、保留容量（承接 u3-l2）
  }
  // → 分配次数 = line 增长时的重配次数，与行数无关
```

关键点：

- `read_line(&mut line)` 把一行**追加**进 `line`（不覆盖），所以循环末尾必须 `clear()`，否则下一行会接在上一行尾巴上。
- 一旦最长一行的长度触顶，`line` 的容量就稳定下来，后续再无分配——这正是 u3-l2 讲过的「workhorse 集合 + `clear()` 复用容量」。
- **前提条件**：循环体必须能接受 `&str` 而非 `String`。如果你的处理逻辑需要拥有/修改这一行，这个优化就不直接适用。

#### 4.3.3 源码精读

io.md 把读者引向 heap-allocations.md（[src/io.md:99-105](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L99-L105)），真正的对照代码在 [src/heap-allocations.md:374-417](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L374-L417)。

会分配的 `lines()` 写法见 [src/heap-allocations.md:376-389](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L376-L389)：

```rust
use std::io::{self, BufRead};
let mut lock = io::stdin().lock();
for line in lock.lines() {
    process(&line?);
}
```

书里点明它的代价：这个迭代器产出 `io::Result<String>`，「**意味着它为文件中的每一行都做一次分配**」。

workhorse `String` 写法见 [src/heap-allocations.md:393-407](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L393-L407)：

```rust
use std::io::{self, BufRead};
let mut lock = io::stdin().lock();
let mut line = String::new();
while lock.read_line(&mut line)? != 0 {
    process(&line);
    line.clear();
}
```

书对收益的精确刻画见 [src/heap-allocations.md:408-410](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L408-L410)：它把分配次数降到「**至多少数几次，可能只有一次**」，确切次数取决于 `line` 需要重配几次，而这又取决于文件里行长度的分布（最长那行决定最终容量）。

最后一条约束见 [src/heap-allocations.md:412-413](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md#L412-L413)：「这只有在循环体能处理 `&str` 而非 `String` 时才成立」——若下游需要拥有数据，就得另想办法（比如 Cow，见 u3-l1）。

> 串起来看：这里的 `String::new()` + `clear()` 就是 u3-l2 的 workhorse 模式；而 `stdin().lock()` 又是本讲 4.1 的手动锁定。一条「逐行读 stdin」的热路径，同时用到了本讲与 u3-l2 的两个优化。

#### 4.3.4 代码实践

1. **实践目标**：用 dhat-rs（承接 u3-l1/u3-l2）量化 `lines()` 与 workhorse `String` 的分配次数差异。
2. **操作步骤**（示例代码，需 `cargo add dhat`）：
   ```rust
   // 示例代码：src/main.rs
   use std::io::{self, BufRead};

   fn main() -> io::Result<()> {
       // 准备一个多行的 stdin（用 `yes | head -n 1000000` 之类喂入，或直接读文件）
       // 这里以读 stdin 为例，对比两种读法（每次只启用一种）

       // 读法 A：lines() —— 每行分配一个 String
       // let _prof = dhat::Profiler::new_heap();
       // for line in io::stdin().lock().lines() { let _ = line?; }

       // 读法 B：workhorse String
       let _prof = dhat::Profiler::new_heap();
       let mut lock = io::stdin().lock();
       let mut line = String::new();
       while lock.read_line(&mut line)? != 0 {
           line.clear();
       }
       Ok(())
   }
   ```
   - 用一个百万行输入分别跑两版（用 `seq 1000000 | ./target/release/<crate>` 之类喂数据）。
   - 查看 dhat 结束时打印的 `Total blocks`（分配块数）。
3. **需要观察的现象**：读法 A 的 `Total blocks` 约等于行数（百万级）；读法 B 的 `Total blocks` 只有个位数。
4. **预期结果**：分配次数从「与行数成正比」降到「近乎常数」，与书中「至多几次、甚至一次」一致。
5. **说明**：确切数字取决于行长分布，待本地验证；若无 dhat，退而用 DHAT（Valgrind 工具）或一个简单的全局分配计数器观察量级差异即可。

#### 4.3.5 小练习与答案

**练习 1**：`read_line(&mut line)` 之后为什么必须 `line.clear()`？不 `clear()` 会怎样？

> **参考答案**：`read_line` 是**追加**写入 `line`，不会先清空。不 `clear()` 的话，第二行的内容会接在第一行后面，`line` 越积越长，逻辑错误且分配只会增不会复用。`clear()` 把长度归零但**保留容量**，正是 workhorse 模式复用缓冲的关键（承接 u3-l2）。

**练习 2**：为什么说 workhorse `String` 的分配次数「与行数无关，而与行长分布有关」？

> **参考答案**：因为 `line` 只在容量不够时才重配，而容量只会单调增长到「能容纳最长一行」。一旦最长行被读过、容量触顶，后续再短的行也只需 `clear()`、零分配。所以决定分配次数的是「过程中容量需要扩几次」（由行长分布决定），不是行数。

---

### 4.4 按字节读入原始输入：跳过 UTF-8 校验

#### 4.4.1 概念说明

Rust 内建的 `String`（以及读入它的 `BufRead::read_line`/`lines`）**内部用 UTF-8** 表示文本。当你把输入读进 `String` 时，标准库会**校验**这些字节确实是合法 UTF-8。这层校验开销虽小但**非零**，且在大输入量下会累积。

如果你的程序其实**只关心字节的值、并不需要 UTF-8 语义**——典型情形是处理纯 ASCII 文本——你完全可以绕开这层校验，把输入当作**原始字节**来处理。书给出的标准做法是 [`BufRead::read_until`]：像 `read_line` 一样按分隔符切分，但读入的是字节序列（`Vec<u8>`/`&[u8]`）而非 `String`，不做 UTF-8 校验。

此外还有两个专门的 crate：

- 读「按字节的行」：[rust-linereader](https://github.com/Freaky/rust-linereader)。
- 处理「字节串（byte strings）」：[bstr](https://github.com/BurntSushi/bstr)。

#### 4.4.2 核心流程

两条读取路径的对比：

```text
按字符串读（带 UTF-8 校验）：
  read_line / lines  ──▶ 校验字节是否合法 UTF-8 ──▶ String/&str

按字节读（无校验）：
  read_until(delim)  ──▶ 直接搬运字节 ──▶ Vec<u8> / &[u8]
  （或用 bstr / linereader 处理字节串/字节行）
```

要点：

- `read_until(delimiter, &mut buf)` 与 `read_line(&mut buf)` 用法对称：都是「读到某个分隔符为止、追加进 `buf`」，差别只在分隔符是字节（如 `b'\n'`）、产物是字节缓冲，且**不做 UTF-8 校验**。
- 选择它有一个隐含前提：你的下游逻辑**能处理 `&[u8]`**。如果之后还是非要 `str`/`String`，那 UTF-8 校验迟早要补回来，省下的就有限了。
- 这是典型的「候选优化」：ASCII 处理等场景值得，是否真有收益要靠基准测试确认。

#### 4.4.3 源码精读

书对 UTF-8 校验开销与 `read_until` 的说明见 [src/io.md:109-112](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L109-L112)：

> The built-in `String` type uses UTF-8 internally, which adds a small, but nonzero overhead caused by UTF-8 validation when you read input into it. If you just want to process input bytes without worrying about UTF-8 (for example if you handle ASCII text), you can use `BufRead::read_until`.

对专用 crate 的指引见 [src/io.md:117-118](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L117-L118)，分别指向 rust-linereader（按字节的行）与 bstr（字节串）。这两个链接在书稿中是 [src/io.md:119-120](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/io.md#L119-L120) 的引用定义。

#### 4.4.4 代码实践

1. **实践目标**：用 `read_until` 按 `b'\n'` 切分、以字节方式统计行数，绕开 UTF-8 校验。
2. **操作步骤**（示例代码）：
   ```rust
   // 示例代码
   use std::io::{self, BufRead};

   fn main() -> io::Result<()> {
       let mut lock = io::stdin().lock();
       let mut buf: Vec<u8> = Vec::new();   // workhorse 字节缓冲（同 4.3 思路）
       let mut count = 0u64;
       while lock.read_until(b'\n', &mut buf)? != 0 {
           // 按 ASCII 字节处理：以换行符为行尾统计行数
           if buf.last() == Some(&b'\n') { count += 1; }
           buf.clear();
       }
       eprintln!("lines: {count}");
       Ok(())
   }
   ```
   - 用大段 ASCII 输入（如 `seq 5000000 | ...`）分别跑 `read_line`（4.3 读法 B）与本版 `read_until`，用 Hyperfine 对比。
3. **需要观察的现象**：在纯 ASCII 大输入下，`read_until` 版本通常略快（省去了 UTF-8 校验）；若输入含大量多字节 UTF-8，差距形态会不同。
4. **预期结果**：ASCII 场景下字节路径有可测量的（但不大）优势，与书里「small, but nonzero overhead」一致。
5. **说明**：UTF-8 校验在现代实现里相当快，收益未必显著；务必以本地基准测试为准，不要默认「字节一定更快」。

#### 4.4.5 小练习与答案

**练习 1**：什么场景下「跳过 UTF-8 校验」是安全的？什么场景下不安全？

> **参考答案**：当你**只处理字节的值**（如按 ASCII 字节统计、按字节查找分隔符、处理二进制协议）时安全——你本就不依赖「这些字节构成合法字符串」。当你后续需要把这些字节当**文本**用（切片取 char、调用字符串方法、保证显示正确）时就不安全——非 UTF-8 字节会在转 `str` 时出错，或导致错误的结果。

**练习 2**：`read_until(b'\n', &mut buf)` 和 `read_line(&mut line)` 在「追加写入」这件事上是否一致？

> **参考答案**：一致。两者都是**追加**进传入的缓冲、读到分隔符（或 EOF）为止，因此循环里同样需要 `buf.clear()`/`line.clear()` 来复用 workhorse 缓冲。差别仅在分隔符类型（字节 vs 行）、产物类型（字节 vs 字符串）、以及是否做 UTF-8 校验。

---

## 5. 综合实践

把本讲四个模块串起来，完成规格里要求的主任务：**把一段循环里反复 `println!` 的代码改写为先 `stdout().lock()` 再用 `writeln!` 输出；若写文件则包一层 `BufWriter`，比较前后性能**。在此基础上，把「锁 + 缓冲」组合、以及「按字节读」也纳入，形成一个完整的 I/O 优化闭环。

1. **实践目标**：在一个程序里同时实践「手动锁定」「缓冲」「锁+缓冲组合」「按字节读」，并用基准测试量化每一步的收益。
2. **操作步骤**（示例代码）：
   ```rust
   // 示例代码：src/main.rs
   use std::io::{self, BufRead, BufWriter, Write};

   fn emit_stdout(lines: &[String]) -> io::Result<()> {
       // 锁 + 缓冲 的组合（4.1 + 4.2）：把锁定的 stdout 再包一层 BufWriter
       let stdout = io::stdout();
       let mut out = BufWriter::new(stdout.lock());   // 锁一次 + 全缓冲
       for line in lines {
           writeln!(out, "{}", line)?;
       }
       out.flush()?;                                  // 显式 flush，暴露刷盘错误
       Ok(())
   }

   fn write_file(lines: &[String]) -> io::Result<()> {
       let mut out = BufWriter::new(std::fs::File::create("/tmp/out.txt")?);
       for line in lines {
           writeln!(out, "{}", line)?;
       }
       out.flush()?;
       Ok(())
   }

   fn count_lines_bytes() -> io::Result<u64> {
       // 按字节读 stdin（4.4），workhorse 字节缓冲复用（4.3 思路）
       let mut lock = io::stdin().lock();
       let mut buf: Vec<u8> = Vec::new();
       let mut count = 0u64;
       while lock.read_until(b'\n', &mut buf)? != 0 {
           if buf.last() == Some(&b'\n') { count += 1; }
           buf.clear();
       }
       Ok(count)
   }

   fn main() -> io::Result<()> {
       let lines: Vec<String> = (0..500_000).map(|i| format!("line {i}")).collect();
       emit_stdout(&lines)?;
       write_file(&lines)?;
       // 喂入大段 ASCII 后可观察： eprintln!("stdin lines: {}", count_lines_bytes()?);
       Ok(())
   }
   ```
   - 准备**对照基线**：`emit_stdout` 的「无锁无缓冲」版（直接 `for line { println!("{}", line); }`）、`write_file` 的「无缓冲」版（直接写 `File`）。
   - 用 Hyperfine 分别对比「基线 vs 手动锁定 vs 锁+缓冲」三档，输出**重定向到文件**（避免终端渲染瓶颈）。
   - 用 `strace -c` 观察 `write` 系统调用次数随缓冲的变化；用 dhat（4.3.4）观察 `read_until` workhorse 缓冲的分配次数。
3. **需要观察的现象**：
   - stdout 重定向到文件时，手动锁定快于反复 `println!`；再加 `BufWriter` 后 `write` 系统调用次数骤降、进一步加速。
   - 写文件时，`BufWriter` 版的 `write` 系统调用次数远少于裸 `File` 版。
   - 按字节读时，workhorse `Vec<u8>` 的分配次数为个位数。
4. **预期结果**：三个优化维度（锁、缓冲、按字节读）各自带来可测量的收益，且彼此**正交**、可叠加。
5. **说明**：所有倍数待本地验证；尤其注意「输出到终端」会让锁/缓冲的收益被终端渲染掩盖——务必重定向。整个过程再次印证贯穿本册的纪律：**写法改动只是候选优化，收益必须靠基准测试与系统调用/分配统计来确认。**

---

## 6. 本讲小结

- `print!`/`println!` **每次调用都锁 stdout**；循环里反复调用时，改成循环前 `stdout().lock()`、循环内 `writeln!(lock, ...)`，把 \( n \) 次锁摊还成 1 次。stdin/stderr 同理可锁。
- Rust 文件 I/O **默认不缓冲**；`BufReader`/`BufWriter` 在内存里攒批，**最小化系统调用次数**。显式 `flush()?` 的价值不在「让它刷新」（drop 时本会自动 flush），而在**不让刷盘错误被吞掉**。
- 读写缓冲**非对称**：写两侧都实现 `Write`、加缓冲几乎不改代码（所以最易被忘）；读一侧从 `Read` 切到 `BufRead`、代码会变样（所以很少被漏）。
- 逐行读文件有**分配取舍**：`BufRead::lines()` 每行分配一个 `String`；用 workhorse `String` + `read_line` + `clear()` 可把分配压到「至多几次、甚至一次」——这是 u3-l2 workhorse 模式在 I/O 上的直接应用，前提是循环体能吃 `&str`。
- 读入 `String` 会带来**UTF-8 校验开销**；若只关心字节值（如处理 ASCII），可用 `BufRead::read_until` 或 `bstr`/`linereader` 按**原始字节**处理，跳过校验。
- 「锁」与「缓冲」是**正交**的两个维度：往 stdout 大量写入时可把 `stdout().lock()` 再包一层 `BufWriter`，同时省锁、省系统调用。

---

## 7. 下一步学习建议

- **包装类型的访问开销**：本讲聚焦 I/O 的锁与缓冲；下一讲 **u5-l3 Wrapper Types 与日志/调试** 会讲 `RefCell`/`Mutex` 等包装类型**每次访问**的开销，与本讲的「锁」主题在更细粒度上呼应，并可一起评估「这把锁到底要不要、能不能合并」。
- **回到分配视角**：4.3 用到的 workhorse `String` + `clear()` 来自 **u3-l2**，若想补全「分配率为何重要、如何用 dhat-rs 写分配回归测试」，可回看 u3-l2 与 **u3-l1**。
- **延伸阅读**：直接阅读 [src/io.md](src/io.md) 原文及其引用的两个真实 PR（rust-lang/rust#93954、dhat-rs#22），以及 [src/heap-allocations.md](src/heap-allocations.md) 的「Reading Lines from a File」一节，体会这些手法在真实工程里的应用。
