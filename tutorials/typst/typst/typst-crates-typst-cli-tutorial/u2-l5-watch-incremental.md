# Watch 模式与增量重编译

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `typst watch` 命令的**主循环五步骤**（更新监控依赖 → 等待变更 → 重置编译状态 → 重新编译 → 驱逐缓存），并解释每一步为什么要这么做。
- 理解 watch 如何在「文件还不存在」时**阻塞等待**并周期性重试，而不是直接报错退出。
- 读懂 `Status` 枚举（Compiling / Success / PartialSuccess / Error）如何驱动终端的状态显示、耗时统计与（启用时）HTTP 服务器地址的打印。
- 解释 `warn_watching_std` 为何要警告「stdin 无法被 watch」，以及 `watching` 配置项在 `CompileConfig` 中的作用。
- 能够亲手运行 `typst watch`，通过修改主文件与 `#import` 依赖文件来观察自动重编译，并把现象对应到 `watcher.update(world.dependencies())` 监控的文件集合。

本讲承接 [u2-l2 编译配置与单次编译](u2-l2-compile-config.md) 的 `compile_once`/软失败机制，以及 [u2-l3 多格式导出](u2-l3-multi-format-export.md) 的 `ExportCache`。如果说 `compile_once` 是「编译一次」，那么 `watch` 就是「把 `compile_once` 装进一个永不退出的循环」。

## 2. 前置知识

在阅读本讲前，建议你先了解以下概念（不熟悉的术语下面会解释）：

- **软失败（soft failure）**：编译出错时 `compile_once` 仍然返回 `Ok(())`，只是通过 `set_failed()` 把进程退出码改成非 0。这是 watch 能「错了继续盯着」的前提。详见 u1-l2、u2-l2。
- **依赖（dependency）**：一次编译过程中真正被读取过的文件——主文件、`#include`/`#import` 的文件、`#image(...)`、`#csv(...)` 等引用的资源。注意「依赖」是**编译结果的副产品**：只有被这次编译实际访问到的文件，才算依赖。
- **增量编译（incremental compilation）**：Typst 用 `comemo` 库做记忆化（memoization），把已经算过的中间结果缓存起来，下次输入没变就直接复用，从而只重算变化的部分。
- **文件系统事件（filesystem event）**：操作系统在你读写、创建、删除、重命名文件时产生的事件。Rust 生态里 `notify` crate 把不同操作系统（Linux 的 inotify、macOS 的 FSEvents、Windows 的 ReadDirectoryChangesW）的差异抹平，提供统一的事件流。
- **stdin / 标准输入**：用 `-` 表示从管道读入。管道不是磁盘文件，操作系统**不会**对它产生文件变更事件，所以 stdin 天然无法被 watch——这是本讲一个关键边界的来源。

## 3. 本讲源码地图

本讲主要涉及以下文件。`src/watch.rs` 是主角，其余文件提供它调用的能力。

| 文件 | 作用 |
| --- | --- |
| `src/watch.rs` | watch 命令的全部实现：主循环、`Status` 状态显示、stdin 警告。本讲核心。 |
| `src/compile.rs` | 提供 `compile_once`、`CompileConfig`（含 `watching`/`export_cache`/`server`）、`print_diagnostics`。watch 循环每轮都调用它。 |
| `crates/typst-kit/src/watcher.rs`（同级工作区 crate） | `Watcher` 类型：封装 `notify`，提供 `update()`（更新要监控的文件集合）和 `wait()`（阻塞等待相关事件）。 |
| `crates/typst-kit/src/files.rs`（同级工作区 crate） | `FileStore`：负责文件加载缓存、`dependencies()`（返回上次编译访问的文件）、`reset()`（把缓存标记为陈旧）。 |
| `crates/typst-kit/src/datetime.rs`（同级工作区 crate） | `Time` 类型：提供 `World::today()`，`reset()` 让每次编译重新取系统时间。 |
| `src/world.rs` | `SystemWorld`：把上面 `FileStore`/`Time`/字体包装成编译器要的 `World`，并提供 `dependencies()`/`reset()`/`scan_fonts()`。 |

> 说明：`watcher.rs`、`files.rs`、`datetime.rs` 属于 `typst-kit` 这个**兄弟 crate**（CLI 依赖它，复用其通用能力），所以它们的永久链接前缀是 `…/crates/typst-kit/`，而不是 `…/crates/typst-cli/`。

## 4. 核心概念与源码讲解

### 4.1 watch 主循环：监控、等待、重置、重编译、驱逐缓存

#### 4.1.1 概念说明

`typst compile` 编译一次就退出；`typst watch` 则要**持续运行**：它盯着磁盘上的文件，一旦有变化就重新编译，直到你按 Ctrl+C 中断。

要做这件事，最朴素的思路是「死循环里不停重新编译」。但这有两个问题：

1. **白干**：如果文件根本没变，全量重编译是浪费；如果变了，又得知道是哪些文件变了。
2. **忙等**：不停循环会吃满 CPU。

`watch.rs` 的解法是「**事件驱动 + 增量**」：用操作系统的文件事件来唤醒（不忙等），每次唤醒后只让编译器重算受影响的部分。这就需要一个能「监听文件」的组件（`Watcher`）、一个能「报告上次编译读了哪些文件」的组件（`FileStore`）、以及一个能「清理记忆化缓存防止内存无限增长」的动作（`comemo::evict`）。

#### 4.1.2 核心流程

watch 的主循环可以概括为五步，每轮执行一次：

```
┌─ 进入循环 ────────────────────────────────────────────────┐
│  ① watcher.update(world.dependencies())                    │
│        用「上次编译实际读过的文件」刷新监听清单              │
│  ② watcher.wait()                                          │
│        阻塞，直到某个被监听文件发生「相关」事件              │
│  ③ world.reset()                                           │
│        把文件缓存标记为陈旧、重置时间，准备重编译            │
│  ④ compile_once(world, config)                             │
│        重新编译并导出（出错也是软失败，循环继续）            │
│  ⑤ comemo::evict(10)                                       │
│        驱逐记忆化缓存，只留最近 10 项，控制内存              │
└──────────────────────────────────────────────────────────────┘
```

注意一个关键点：**监控清单是「编译之后」才知道的**。第一轮编译前，我们还不知道这个文档依赖哪些文件；编译过一次后，`world.dependencies()` 才能告诉我们答案，于是第 ① 步把这些文件加进监听。这就是为什么循环里 `update` 排在 `wait` 之前——它用的是「上一轮」的依赖结果。

#### 4.1.3 源码精读

整个 watch 函数的骨架在 [src/watch.rs:L18-L84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L18-L84)。先看后半段——真正的无限循环：

```rust
// src/watch.rs:68-83
loop {
    // Watch all dependencies of the most recent compilation.
    watcher.update(world.dependencies())?;
    // Wait until anything relevant happens.
    watcher.wait()?;
    // Reset all dependencies.
    world.reset();
    // Recompile.
    timer.record(&mut world, |world| compile_once(world, &mut config))??;
    // Evict the cache.
    comemo::evict(10);
}
```

逐行解读：

- `watcher.update(world.dependencies())`：`world.dependencies()` 返回上一轮编译访问过的真实磁盘路径（见下面「依赖从哪来」）；`Watcher::update` 把监听集合**精确同步**成这份清单——新出现的文件加监听，不再需要的取消监听。两处都用了 `?`，监听失败会让 `watch` 返回错误退出。
- `watcher.wait()`：阻塞当前线程，直到监听到一次「相关」事件。注意 `wait` 内部会**把短时间内的多个事件合并成一次唤醒**，以应对编辑器「先删后写」等行为（详见 4.1 节末尾）。
- `world.reset()`：见 [src/world.rs:L104-L107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L104-L107)：

  ```rust
  pub fn reset(&mut self) {
      self.files.reset();
      self.now.reset();
  }
  ```

  它做两件事：把所有已加载文件标记为「陈旧」（下次访问时重新从磁盘读，但尽量**就地编辑**复用旧 `Source` 以利增量）；重置时间缓存（让本次编译能取到「现在」的时刻，而不是复用上一轮的时间）。
- `timer.record(...)??.`：`compile_once` 被包了一层计时。**两个 `?`** 是重点：
  - 外层 `?` 处理 `Timer::record` 自身的错误（例如计时文件写盘失败）。
  - 内层 `?` 处理 `compile_once` 返回的错误。
  
  为什么是两层？因为 `Timer::record` 的签名是 `StrResult<T>`，而这里的 `T` 恰好是 `compile_once` 的返回类型 `HintedStrResult<()>`，于是整体类型是 `Result<Result<(), HintedString>, EcoString>`，需要两次解包。
  
  **更重要的是**：`compile_once` 只有在「打印诊断失败」或「写依赖文件失败」这类致命错误时才返回 `Err`；普通的编译语法错误是**软失败**（返回 `Ok(())` + `set_failed()`）。所以这条 `??` **不会**因为你的 `.typ` 写错语法而跳出循环——它只会继续下一轮。这正是 watch「写错了也一直盯着帮你改」的底气。
- `comemo::evict(10)`：`comemo` 是 Typst 的增量/记忆化引擎。`evict(10)` 表示**驱逐记忆化缓存，只保留最近使用的 10 项**。watch 是一个可能跑很久的进程，如果完全不清理，每次编译产生的中间缓存会让内存持续膨胀；但又不能全清（全清会让「几乎没变」的下一轮失去复用机会，拖慢重编译）。留 10 项是个折中。

**依赖从哪来**：`SystemWorld::dependencies()` 把内部的 `FileId` 翻译回磁盘路径，见 [src/world.rs:L98-L101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L98-L101)：

```rust
pub fn dependencies(&mut self) -> impl Iterator<Item = PathBuf> + '_ {
    let (loader, deps) = self.files.dependencies();
    deps.filter_map(|id| loader.resolve(id).ok())
}
```

它依赖 `FileStore::dependencies()`（[crates/typst-kit/src/files.rs:L91-L99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L91-L99)），后者返回「自上次 `reset()` 以来被访问过的文件」：

```rust
pub fn dependencies(&mut self) -> (&L, impl Iterator<Item = FileId> + '_) {
    let iter = self.slots.get_mut().iter()
        .filter(|(_, slot)| slot.accessed())   // 只看「被访问过」的槽位
        .map(|(&id, _)| id);
    (&self.loader, iter)
}
```

「被访问过」由 `FileSlot::accessed()`（[crates/typst-kit/src/files.rs:L163-L165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L163-L165)）判断——只要不是初始的 `Empty` 状态就算。这就把「依赖」精确限定为**这次编译真正读到的文件**，而不是「项目目录里所有文件」。这也解释了实践任务里你会观察到的现象：改一个没被引用的文件不会触发重编译。

#### 4.1.4 代码实践

**实践目标**：亲眼看一遍「修改依赖文件 → 自动重编译」，并理解 `watcher.update(world.dependencies())` 监控的到底是哪些文件。

**操作步骤**：

1. 在一个空目录准备两个文件。
   `main.typ`：
   ```typ
   #import "lib.typ": greeting
   #greeting()
   ```
   `lib.typ`：
   ```typ
   #let greeting() = [Hello, watch!]
   ```
2. 在该目录运行（用你在 u1-l1 构建出的二进制）：
   ```bash
   ./target/debug/typst watch main.typ
   ```
3. 终端会清屏并显示 `watching …` / `writing to …` 状态块，给出首次编译耗时。
4. **保持 watch 运行**，在另一个编辑器里把 `main.typ` 的 `#greeting()` 改成 `#greeting() Second.`，保存。
5. 再把 `lib.typ` 里的文案改成 `[Hello, UPDATED!]`，保存。
6. 另建一个**未被引用**的文件 `unused.typ`，随便写点内容，保存。

**需要观察的现象**：

- 第 4、5 步保存后，watch 应几乎立刻清屏并重新显示 `compiled successfully in …`，输出 PDF 被更新。
- 第 6 步保存 `unused.typ` 后，**不应该**触发重编译（状态块不刷新）。

**预期结果**：第 6 步的现象直接验证了 `world.dependencies()` 只包含「实际被读取的文件」。`main.typ` 通过 `#import` 读了 `lib.typ`，所以两者都在依赖集合里、都被监听；`unused.typ` 从未被这次编译访问，`FileStore` 里没有它的 `accessed` 槽位，自然不在监听清单。

> 如果你没有可运行环境，这属于「待本地验证」的现象；但对照上面 `dependencies()` 的源码，结论是确定的。

#### 4.1.5 小练习与答案

**练习 1**：如果把主循环里 `watcher.update(world.dependencies())` 这一行删掉，watch 还能正常工作吗？为什么？

> **答案**：能「工作」但会退化。初始 `Watcher` 是空监听集合，删掉 `update` 后，`wait()` 将一直阻塞，**永远等不到事件**（除非文件恰好在创建 `Watcher` 时就被加进去，但代码里没有）。即使第一次靠某种方式监听了主文件，后续新增的依赖（比如你新写了一个 `#import`）也不会被监听。`update` 的作用就是每轮把监听集合和「真实依赖」对齐。

**练习 2**：主循环里用的是 `??`（两个问号）。请说明分别在什么情况下会触发，以及普通语法错误会不会触发。

> **答案**：外层 `?` 对应 `Timer::record` 失败（如计时文件无法写盘）；内层 `?` 对应 `compile_once` 返回 `Err`，这只发生在打印诊断或写依赖文件失败等致命情况。普通 `.typ` 语法错误是软失败（`compile_once` 返回 `Ok(())` 并 `set_failed()`），**不会**触发内层 `?`，循环继续。

### 4.2 首次编译准备：字体预扫描与「文件不存在」的等待

#### 4.2.1 概念说明

主循环开始之前，`watch` 还要处理两件「进入循环前」的准备工作，它们都和「首次体验」有关：

1. **字体预扫描**：字体发现耗时高度依赖系统，可能从几毫秒到几秒不等。如果这部分时间算进「编译耗时」，用户会困惑「为什么有时快、有时慢」。所以 watch 在首次编译前**抢先**把字体扫完，让显示出的编译耗时更纯粹。
2. **文件不存在的阻塞等待**：如果你 watch 的文件**当前还不存在**（比如你先启动 watch，再去创建文件），`compile` 命令会直接报错退出；但 watch 选择「**等下去**」——周期性地检查文件是否已经被创建出来。

#### 4.2.2 核心流程

构造 `SystemWorld` 的循环（[src/watch.rs:L31-L49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L31-L49)）：

```
loop {
    尝试 SystemWorld::new(...)
      ├─ 成功 → break，进入后续
      ├─ 失败且是 InputNotFound / RootNotFound
      │     → 把这个不存在的路径加入监听
      │     → 打印 Status::Error
      │     → 打印错误
      │     → watcher.wait() 阻塞等待
      │     （wait 内部会周期性轮询「缺失文件」是否出现）
      └─ 其他错误 → 直接 return Err 退出 watch
}
```

`Watcher::wait()` 对「缺失文件」有专门的处理：它不会无限期等一个操作系统事件（因为不存在的文件不会产生事件），而是改用**轮询**，每隔 `POLL_INTERVAL`（300ms）检查一次这些「缺失」文件是否已经被创建。

#### 4.2.3 源码精读

先看字体预扫描与首次编译，[src/watch.rs:L55-L60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L55-L60)：

```rust
if config.output_format.is_paged() {
    world.scan_fonts();
}
// Perform initial compilation.
timer.record(&mut world, |world| compile_once(world, &mut config))??;
```

`scan_fonts()` 只是强制触发惰性的字体发现（[src/world.rs:L112-L114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L112-L114)）：

```rust
pub fn scan_fonts(&mut self) {
    LazyLock::force(&self.fonts);
}
```

注意它有条件：只有 `output_format.is_paged()`（PDF/PNG/SVG 这类会真正用到字体的分页格式）才预扫。HTML/Bundle 走不同目标类型，未必需要，故跳过以省时间。预扫之后，首次 `compile_once` 显示的耗时就不含字体发现的时间了。

再看「文件不存在」的等待循环，[src/watch.rs:L31-L49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L31-L49)：

```rust
let mut world = loop {
    match SystemWorld::new(Some(&command.args.input), &command.args.world, &command.args.process) {
        Ok(world) => break world,
        Err(ref err @ (WorldCreationError::InputNotFound(ref path)
            | WorldCreationError::RootNotFound(ref path))) => {
            watcher.update([path.clone()])?;
            Status::Error.print(&config).unwrap();
            print_error(&err.to_string()).unwrap();
            watcher.wait()?;
        }
        Err(err) => return Err(err.into()),
    }
};
```

要点：

- 只对 `InputNotFound`（输入文件不存在）和 `RootNotFound`（项目根不存在）这两种「文件迟早会出现」的错误做等待；其它错误（如时间戳非法）直接 `return Err` 退出。
- 等待时先 `Status::Error.print` 刷出错误状态块，再用 `print_error` 打印具体原因，最后 `watcher.wait()` 阻塞。这里的 `watcher.update([path.clone()])` 把缺失路径登记进 `Watcher` 的 `missing` 集合，使 `wait` 知道要轮询它。

`wait()` 的轮询逻辑在 [crates/typst-kit/src/watcher.rs:L121-L192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L121-L192)。关键是开头这段（L126-L130）：

```rust
let first = self.rx.recv_timeout(if self.missing.is_empty() {
    Duration::MAX
} else {
    Self::POLL_INTERVAL   // 300ms
});
```

以及结尾的判定（L188-L190）：

```rust
if relevant || self.missing.iter().any(|path| path.exists()) {
    return Ok(());
}
```

含义：如果存在「缺失文件」，`wait` 就用 300ms 的超时来收事件，而不是无限等待；每轮结束时检查缺失文件是否已经出现（`path.exists()`），出现了就唤醒去重试构造 world。

最后，`watcher.wait()` 还有一个精巧设计——**事件批处理**，用以对抗编辑器的「保存=先删后写/重命名」行为。相关常量在 [crates/typst-kit/src/watcher.rs:L38-L45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L38-L45)：

```rust
const BATCH_TIMEOUT: Duration = Duration::from_millis(100);
const STARVE_TIMEOUT: Duration = Duration::from_millis(500);
const POLL_INTERVAL: Duration = Duration::from_millis(300);
```

`wait` 收到第一个相关事件后，会继续在 `BATCH_TIMEOUT`（100ms）窗口内收集后续事件并合并；同时用 `STARVE_TIMEOUT`（500ms）设上限，避免事件风暴让重编译一直「饿着」排不上。时间线大致如下：

```
事件1 ─┐  (100ms 内还有事件就继续攒)
事件2 ─┤
事件3 ─┘──> 合并为一次唤醒 ──> 重编译   (累计不超过 500ms)
```

#### 4.2.4 代码实践

**实践目标**：验证 watch 对「文件尚不存在」的容忍。

**操作步骤**：

1. 确保目标文件 `notyet.typ` **不存在**。
2. 运行：
   ```bash
   ./target/debug/typst watch notyet.typ
   ```
3. 观察：终端应显示错误状态（`compiled with errors`）并打印 `input file not found …`，然后**停在那里不退出**。
4. 在另一个终端 `touch notyet.typ`（或写入内容 `Hello.`）。

**需要观察的现象**：第 4 步之后，watch 应自动检测到文件出现，进入正常编译并显示成功状态。

**预期结果**：这正是 [src/watch.rs:L42-L45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L42-L45) 把缺失路径交给 `watcher.wait()` 轮询的效果。对照 `compile` 命令（一次性）会直接报错退出，体会两者策略差异。

> 待本地验证：具体多久检测到取决于 `POLL_INTERVAL`（300ms）。

#### 4.2.5 小练习与答案

**练习 1**：为什么字体预扫描要放在「首次编译之前」，而不是懒到第一次用到时？

> **答案**：字体发现耗时随系统差异很大。若放任它惰性发生在首次编译中，显示出的「编译耗时」就会被字体发现时间污染，导致用户疑惑「为什么这次编译这么慢」。提前扫掉，能让状态块里显示的耗时更真实地反映编译本身。

**练习 2**：`SystemWorld::new` 返回 `InvalidTimestamp`（时间戳非法）时，watch 会怎么做？

> **答案**：会直接 `return Err(err.into())` 退出 watch（见 [src/watch.rs:L47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L47)）。因为时间戳非法不是「等一等就会好」的问题，继续等也没用，所以不像文件不存在那样重试。

### 4.3 Status 状态显示与终端清屏

#### 4.3.1 概念说明

watch 是持续运行的，用户需要一个**稳定的状态区**来知道「现在在编译 / 编译成功 / 有警告 / 出错了 / 花了多久」。每次状态变化，watch 会**清屏后重绘**整个状态块，保证信息始终在屏幕顶部、不会和历史输出混在一起。

状态分四种，由 `Status` 枚举表示：

| 变体 | 含义 | 何时显示 |
| --- | --- | --- |
| `Compiling` | 正在编译 | 每轮编译开始前 |
| `Success(耗时)` | 编译成功、无警告 | 编译成功且无警告 |
| `PartialSuccess(耗时)` | 编译成功但有警告 | 编译成功且有警告 |
| `Error` | 编译出错 | 编译失败（软失败） |

#### 4.3.2 核心流程

`Status::print` 的渲染流程：

1. 取当前本地时间作为时间戳（`%H:%M:%S`）。
2. 根据 `Status` 选择颜色（错误=红/错误色、部分成功=警告色、其余=普通色）。
3. 获取 `terminal::out()`（带颜色的终端输出单例），**清屏**。
4. 依次打印：`watching <输入>`、`writing to <输出>`、（启用 http-server 且格式为 HTML/Bundle 时）`serving at http://<地址>`、空行、`[时间戳] <状态消息>`。

状态消息由 `Status::message()` 生成；颜色由 `Status::color()` 从 `codespan_reporting` 的默认样式中取。

#### 4.3.3 源码精读

`Status` 枚举与 `print`，[src/watch.rs:L87-L129](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L87-L129)。先看枚举和消息：

```rust
pub enum Status {
    Compiling,
    Success(std::time::Duration),
    PartialSuccess(std::time::Duration),
    Error,
}
```

`print` 的关键部分（L96-L128）：

```rust
pub fn print(&self, config: &CompileConfig) -> io::Result<()> {
    let timestamp = chrono::offset::Local::now().format("%H:%M:%S");
    let color = self.color();
    let mut out = terminal::out();
    out.clear_screen()?;                 // 先清屏

    out.set_color(&color)?;
    write!(out, "watching")?;
    out.reset()?;
    match &config.input {
        Input::Stdin => writeln!(out, " <stdin>"),
        Input::Path(path) => writeln!(out, " {}", path.display()),
    }?;
    // ... writing to / serving at ...
    writeln!(out, "[{timestamp}] {}", self.message())?;
    out.flush()
}
```

几个要点：

- **清屏**：`out.clear_screen()` 来自 `terminal.rs` 的 `TermOut`（见 u2-l4）。watch 每次状态变化都先清屏，所以状态块永远在顶部。这也是为什么 watch 不适合把诊断信息「堆积」——诊断会在状态块下方打印，下一轮又会被清掉。
- **HTTP 服务器地址**：这段是条件编译，[src/watch.rs:L116-L122](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L116-L122)：

  ```rust
  #[cfg(feature = "http-server")]
  if let Some(server) = &config.server {
      out.set_color(&color)?;
      write!(out, "serving at")?;
      out.reset()?;
      writeln!(out, " http://{}", server.addr())?;
  }
  ```

  只有启用 `http-server` feature、且 `CompileConfig` 里构造了 `server`（即 watch 输出 HTML/Bundle 且未 `--no-serve`）时，才会打印 `serving at http://…`，提示你浏览器打开的地址。这正是 u4-l3「HTML/Bundle 导出与 http-server」会深入的部分。

`message()` 与 `color()` 在 [src/watch.rs:L131-L151](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L131-L151)：

```rust
fn message(&self) -> String {
    match *self {
        Self::Compiling => "compiling ...".into(),
        Self::Success(d) => format!("compiled successfully in {}", format_duration(d)),
        Self::PartialSuccess(d) => format!("compiled with warnings in {}", format_duration(d)),
        Self::Error => "compiled with errors".into(),
    }
}
fn color(&self) -> termcolor::ColorSpec {
    let styles = term::Styles::default();
    match self {
        Self::Error => styles.header_error,
        Self::PartialSuccess(_) => styles.header_warning,
        _ => styles.header_note,
    }
}
```

颜色取自 `codespan_reporting` 的 `term::Styles::default()`——和编译诊断用的是同一套配色体系，视觉上统一。

那么这些 `Status` 是在哪里被触发打印的呢？答案在 `compile_once` 里（[src/compile.rs:L262-L306](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L262-L306)）：

```rust
let start = std::time::Instant::now();
if config.watching {
    Status::Compiling.print(config).unwrap();
}
let Warned { output, mut warnings } = compile_and_export(world, config);
// ... 合并静态警告 ...
match &output {
    Ok(_) => {
        let duration = start.elapsed();
        if config.watching {
            if warnings.is_empty() {
                Status::Success(duration).print(config).unwrap();
            } else {
                Status::PartialSuccess(duration).print(config).unwrap();
            }
        }
        // ... 打印诊断 ...
    }
    Err(errors) => {
        set_failed();
        if config.watching {
            Status::Error.print(config).unwrap();
        }
        // ... 打印诊断 ...
    }
}
```

注意 **`config.watching` 守卫**：所有 `Status::*.print` 都包在 `if config.watching { … }` 里。也就是说，**只有 watch 模式才会打印这些状态块**；普通 `compile` 走同一段 `compile_once` 代码，但 `watching` 为 `false`，状态块被跳过。这是 `compile` 与 `watch` 复用同一编译函数、又只在 watch 下显示状态的关键开关。耗时 `duration` 也只在 watch 下被采集与显示。

#### 4.3.4 代码实践

**实践目标**：观察四种状态，并验证「普通 compile 不显示状态块」。

**操作步骤**：

1. 用 4.1 实践里的 `main.typ` 运行 watch，记录首次 `compiled successfully in …`。
2. 故意制造一个警告：在 `lib.typ` 里写一行会触发警告的代码（例如引用一个未定义但 Typst 会给出警告的内容，或直接写一个 `#context [deprecated]` 之类——具体取决于 Typst 版本，可参考 `typst compile` 的警告输出）。观察状态变成 `compiled with warnings in …`。
3. 故意制造一个错误：把 `lib.typ` 的 `#let` 行改成 `#let greeting() =`（语法不完整）。观察状态变成 `compiled with errors`，并且**进程没有退出**。
4. 修复错误，观察状态回到 `compiled successfully`。
5. 退出 watch（Ctrl+C），改用 `./target/debug/typst compile main.typ` 编译。

**需要观察的现象**：

- 第 1–4 步：状态在 Compiling → Success/PartialSuccess/Error 之间切换，且出错时不退出。
- 第 5 步：`compile` **没有任何状态块**，只输出诊断（或静默成功）。

**预期结果**：第 5 步印证了 `config.watching` 守卫——`Status::print` 被 `if config.watching` 挡掉了。第 3 步印证了软失败——`Status::Error` 之后 `compile_once` 仍返回 `Ok(())`，主循环的 `??` 不会中断。

> 待本地验证：第 2 步具体能稳定触发警告的写法可能随 Typst 版本变化；若不确定，可直接观察第 3 步的 Error 状态，它最稳定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Status::print` 里那些状态打印要全部包在 `if config.watching { … }` 里，而不是 watch 单独写一份编译函数？

> **答案**：为了让 `compile_once` 这一份「编译 + 导出 + 诊断」逻辑被 `compile` 和 `watch` 完全复用，避免两份代码分叉。`watching` 布尔值是唯一区别：它决定要不要显示状态块、要不要用 `ExportCache`、要不要构造 HTTP 服务器。

**练习 2**：`PartialSuccess` 和 `Success` 的唯一区别是什么？由什么决定显示哪一个？

> **答案**：区别在「有没有警告」。编译成功后，若 `warnings.is_empty()` 显示 `Success`，否则显示 `PartialSuccess`。错误则一律走 `Error` 分支，与警告无关。

### 4.4 watch 配置与「stdin 无法被 watch」的警告

#### 4.4.1 概念说明

最后看两个配置层面的细节：

1. **`CompileConfig::watching`**：watch 命令用 `CompileConfig::watching(command)` 而不是 `CompileConfig::new(command)` 来构造配置。二者共用 `new_impl`，靠第二参数 `watch: Option<&WatchCommand>` 区分。这个参数会触发三件 watch 专属的事：置 `watching = true`、（输出 HTML/Bundle 时）构造 HTTP 服务器、禁止把产物或依赖写到 stdout。
2. **stdin 不能 watch**：watch 的输入必须是磁盘文件，因为 stdin 是管道，操作系统不产生文件事件。如果你用 `typst watch -`，CLI 不会拒绝你（仍能编译 stdin），但会**警告**你「没法监听 stdin 变化」，并提示改用文件或 `typst compile`。

#### 4.4.2 核心流程

watch 函数开头会做一个硬性校验：输出不能是 stdout（因为 watch 要反复覆盖写文件，stdout 做不到）。随后 `CompileConfig::watching` 在 `new_impl` 里完成 watch 专属配置。首次编译之后，如果发现输入是 stdin，就调用 `warn_watching_std` 打印警告。

#### 4.4.3 源码精读

watch 入口先做 stdout 校验（[src/watch.rs:L18-L24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L18-L24)）：

```rust
pub fn watch(command: &'static WatchCommand) -> HintedStrResult<()> {
    let mut timer = Timer::new_or_placeholder(command.args.timings.clone());
    let mut config = CompileConfig::watching(command)?;

    let Output::Path(output) = &config.output else {
        bail!("cannot write document to stdout in watch mode");
    };
    // ...
```

`CompileConfig::watching` 与 `new` 的关系，[src/compile.rs:L93-L100](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L93-L100)：

```rust
pub fn new(command: &CompileCommand) -> HintedStrResult<Self> {
    Self::new_impl(&command.args, None)
}
pub fn watching(command: &WatchCommand) -> HintedStrResult<Self> {
    Self::new_impl(&command.args, Some(command))
}
```

`new_impl` 里 `watch` 专属的 stdout 禁止校验（[src/compile.rs:L211-L222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L211-L222)）：

```rust
match (&output, &deps, watch) {
    (Output::Stdout, _, Some(_)) => {
        bail!("cannot write document to stdout in watch mode");
    }
    (_, Some(Output::Stdout), Some(_)) => {
        bail!("cannot write dependencies to stdout in watch mode")
    }
    (Output::Stdout, Some(Output::Stdout), _) => {
        bail!("cannot write both output and dependencies to stdout")
    }
    _ => {}
}
```

以及 `watching` 标志与 HTTP 服务器构造（[src/compile.rs:L184-L196](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L184-L196) 与 [src/compile.rs:L224-L229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L224-L229)）：

```rust
#[cfg(feature = "http-server")]
let server = if let Some(command) = watch
    && !command.server.no_serve
    && matches!(output_format, OutputFormat::Html | OutputFormat::Bundle)
{
    Some(HttpServer::new(&eco_format!("{input}"), command.server.port, !command.server.no_reload)?)
} else {
    None
};
// ...
Ok(Self { warnings, watching: watch.is_some(), /* … */ server, /* … */ })
```

`WatchCommand` 和 `ServerArgs` 的定义（[src/args.rs:L124-L133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L124-L133) 与 [src/args.rs:L495-L510](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L495-L510)）告诉我们 watch 的命令行参数其实就是 `CompileArgs` 再加一组 `ServerArgs`：

```rust
pub struct WatchCommand {
    #[clap(flatten)]
    pub args: CompileArgs,
    #[cfg(feature = "http-server")]
    #[clap(flatten)]
    pub server: ServerArgs,
}
// ServerArgs: --no-serve / --no-reload / --port
```

最后是 stdin 警告，[src/watch.rs:L63-L65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L63-L65) 与 [src/watch.rs:L155-L164](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L155-L164)：

```rust
if matches!(&config.input, Input::Stdin) {
    warn_watching_std(&world, &config)?;
}
// ...
fn warn_watching_std(world: &SystemWorld, config: &CompileConfig) -> StrResult<()> {
    let warning = warning!(
        Span::detached(),
        "cannot watch changes for stdin";
        hint: "to recompile on changes, watch a regular file instead";
        hint: "to compile once and exit, please use `typst compile` instead";
    );
    print_diagnostics(world, &[], &[warning], config.diagnostic_format)
        .map_err(|err| eco_format!("failed to print diagnostics ({err})"))
}
```

注意这段警告**放在首次编译之后**：也就是说 `typst watch -` 会先把 stdin 编译一次（产出结果），然后才警告「我没法监听它的变化」——此后 `watcher.wait()` 永远等不到 stdin 的事件，watch 实际上就「卡住」了。这与「文件不存在」的轮询不同：stdin 不会被加入 `missing` 集合去轮询，因为 stdin 不是磁盘路径。

#### 4.4.4 代码实践

**实践目标**：体验 watch 的几个配置边界。

**操作步骤**：

1. 尝试把产物写到 stdout：
   ```bash
   echo 'Hi' | ./target/debug/typst watch - -
   ```
   观察是否被 `bail!` 拒绝，对照 [src/watch.rs:L22-L24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L22-L24)。
2. 尝试 stdin 输入但输出到文件：
   ```bash
   echo 'Hello stdin' | ./target/debug/typst watch - out.pdf
   ```
   观察首次编译是否成功生成 `out.pdf`，并是否出现 `cannot watch changes for stdin` 警告，随后进程「卡住」等待。

**需要观察的现象**：

- 第 1 步：立即报错 `cannot write document to stdout in watch mode`，不进入编译。
- 第 2 步：先成功编译一次并写 `out.pdf`，然后打印 stdin 警告，之后停住（因为 stdin 不会产生事件）。

**预期结果**：第 2 步验证了「stdin 能编译但不能 watch」的设计——警告在首次编译后才出现。如果你希望「编译一次 stdin 就退出」，按提示改用 `typst compile - out.pdf`。

> 待本地验证：第 2 步之后 watch 是否真的无限等待，可对照源码确认 `watcher.wait()` 对 stdin 不会轮询。

#### 4.4.5 小练习与答案

**练习 1**：`typst watch - -`（输入输出都是 stdin/stdout）会在哪一步、被哪段代码拒绝？

> **答案**：会在 `watch()` 函数开头 [src/watch.rs:L22-L24](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L22-L24) 处被拒绝（`config.output` 是 `Output::Stdout` 时 `bail!`）。`CompileConfig::new_impl` 里也有一道类似的 stdout 校验（[src/compile.rs:L212-L213](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/compile.rs#L212-L213)），但 `watch()` 自己这道 `let-else` 会先于编译执行。

**练习 2**：为什么不直接在参数解析阶段就禁止 `watch -`，而要等到编译完才警告？

> **答案**：因为「watch stdin」并非完全无意义——它能**编译一次** stdin 并输出结果，只是无法监听后续变化。CLI 选择「先让它编译一次产出结果，再警告用户无法 watch」，把选择权交给用户，而不是粗暴拒绝。这体现了软处理而非硬拒绝的设计取向。

## 5. 综合实践

把本讲的知识串起来，完成下面这个端到端的小任务。

**场景**：你要为一个小型文档项目搭建「保存即刷新」的工作流，并验证 watch 的依赖追踪、状态显示与边界处理。

**任务清单**：

1. 建立如下三文件项目：

   `main.typ`：
   ```typ
   #import "theme.typ": *

   #show: doc => theme(doc)
   = 第一章
   #lorem(20)
   #image("tiger.svg")
   ```

   > 本练习只关注**本地依赖**，故刻意不引入 `@preview` 包，避免网络下载干扰。关键是保留 `#import "theme.typ"`（源码依赖）和 `#image("tiger.svg")`（资源依赖）这两条。

   `theme.typ`：
   ```typ
   #let theme = it => {
     set text(size: 11pt)
     it
   }
   ```
   再准备一个 `tiger.svg`（任意有效 SVG 即可）。

2. 运行 `./target/debug/typst watch main.typ main.pdf`。

3. 依次做以下改动，**每次只改一项**，观察状态块（Compiling/Success/Error）与耗时，并判断是否触发了重编译：
   - 改 `main.typ` 的一处文字 → 预期重编译。
   - 改 `theme.typ` 的字号 → 预期重编译（验证 `#import` 依赖被监听）。
   - 改 `tiger.svg` 内容 → 预期重编译（验证 `#image` 资源被监听）。
   - 新建一个 `scratch.typ` 并写入内容 → 预期**不**重编译（未被引用）。
   - 删除 `main.typ` 再恢复 → 观察状态变化（删除时 world 重置后文件读不到，应报错；恢复后恢复成功）。

4. 打开 `src/watch.rs`，把你每一步观察到的现象，对应到主循环的某一行（`watcher.update` / `watcher.wait` / `world.reset` / `compile_once` / `comemo::evict`）。

**验收标准**：

- 能准确说出「为什么改 `scratch.typ` 不触发」——因为它不在 `world.dependencies()` 返回的集合里。
- 能解释「删除主文件」时 watch 为什么不直接退出——因为文件读取失败在 `compile_once` 里是软失败。
- 能画出 watch 从「保存文件」到「PDF 更新」的完整数据流。

## 6. 本讲小结

- `watch` 的本质是把 `compile_once` 装进一个五步循环：**`watcher.update(world.dependencies())` → `watcher.wait()` → `world.reset()` → `compile_once` → `comemo::evict(10)`**。
- `compile_once` 是**软失败**：编译出错也返回 `Ok(())`，所以主循环的 `??` 只在「打印诊断/写依赖失败」或「计时写盘失败」时才中断，普通语法错误不会让 watch 退出。
- `watcher.update` 每轮用「**上一轮编译实际访问过的文件**」（`FileStore::dependencies` 里 `accessed` 的槽位）刷新监听集合，因此未被引用的文件改动不会触发重编译。
- `watcher.wait` 对「不存在的文件」会退化为 **300ms 轮询**（`POLL_INTERVAL`），并合并 100ms 内的事件（`BATCH_TIMEOUT`），以对抗编辑器「先删后写」行为。
- `Status`（Compiling/Success/PartialSuccess/Error）驱动终端状态块，全部包在 `if config.watching` 守卫里——这是 `compile` 与 `watch` 复用 `compile_once`、又只在 watch 下显示状态的关键开关。
- watch 专属配置由 `CompileConfig::watching`（即 `new_impl(..., Some(command))`）开启：置 `watching=true`、按需构造 HTTP 服务器、禁止写 stdout；stdin 能编译一次但无法被监听，故有 `warn_watching_std` 警告。

## 7. 下一步学习建议

- **HTML/Bundle 与 http-server（u4-l3）**：本讲多次提到 `config.server` 和 `serving at http://…`。如果你想让 watch 配合浏览器实时预览 HTML，下一步应深入 `compile.rs` 的 `export_html`/`export_bundle` 以及 `typst-kit` 的 `HttpServer`，理解 live reload 的注入机制。
- **诊断与终端输出（u2-l4）**：状态块下方的诊断输出、颜色与清屏都依赖 `terminal.rs` 的 `TermOut`。想理解「非 TTY 下颜色为何自动关闭」「short 格式为何更简洁」，可回看 u2-l4。
- **`comemo` 增量原理**：本讲只把 `comemo::evict` 当作「清理缓存」。若想理解 Typst 增量编译到底复用了什么，建议阅读 `comemo` crate 的文档与 `#[comemo::memoize]`/`tracked` 机制，体会「内容寻址的记忆化」如何让只改一行的重编译大幅加速。
- **测试视角（u4-l7）**：watch 的「文件不存在等待」「依赖监听」等行为在 `tests/smoke.rs` 里如何被端到端验证，可参考 smoke 测试的组织方式，尝试为本讲某个边界补一个测试。
