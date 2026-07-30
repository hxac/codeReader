# 文件监视 watcher

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `typst watch` 每次重编译前「监视哪些文件、又何时停止监视哪些文件」的决策过程。
- 用「标记-清除（mark-and-sweep）」准确描述 `Watcher::update` 如何增量地 watch / unwatch 依赖路径，并能解释 `watched` 哈希表里那个布尔值的含义。
- 说清 `Watcher::wait` 的事件批处理机制：`BATCH_TIMEOUT`、`STARVE_TIMEOUT` 各自的作用，以及为什么要把一串事件「攒一波再重编译」。
- 解释当被监视的文件根本不存在时，watcher 如何退化成「定时轮询」。
- 解释为什么 notify-rs 在 Linux 的 inotify 后端上，对文件执行「删除 / 重命名」后必须重新 watch，以及 typst-kit 是怎么自动补救的。

## 2. 前置知识

本讲假设你已经读过以下内容（如果不熟，建议先回看）：

- **u1-l3 模块地图与 World 契约**：知道 `World` trait 是编译器与外界唯一的契约，typst-kit 的各个模块是「拼装 World 的积木」。
- **u3-1 FileStore 与 FileLoader 抽象**：知道 `FileStore` 会用 `dependencies()` 记录「上一次编译访问过哪些 `FileId`」。本讲正是把这一组 `FileId` 喂给 watcher。

此外，本讲会用到三个外部概念，先做最小解释：

- **notify（notify-rs）**：一个跨平台的文件系统事件监听库。你告诉它「请帮我盯着 `a.typ` 这个文件」，之后只要这个文件被修改、删除、重命名，它就会通过一个 channel 给你发一个 `Event`。它的类型 `RecommendedWatcher` 会根据平台自动挑选最合适的后端（Linux 用 inotify、macOS 用 FSEvents、Windows 用 ReadDirectoryChangesW）。
- **增量编译**：Typst 在 `watch` 模式下不是「无脑每隔几秒重编一次」，而是「文件一变就立刻重编」。要做到这一点，编译器必须知道「这次编译读了哪些文件」，然后只盯着这些文件——这正是 watcher 的工作。
- **标记-清除（mark-and-sweep）**：一种经典的垃圾回收思想。本轮先把所有已监视项打上「待清除」标记，再把本轮真正需要的项「重新标记为存活」，最后把仍处于「待清除」状态的项删掉。下面会看到 watcher 几乎是它的教科书式实现。

> 一句话定位：watcher（`src/watcher.rs`，受 `watcher` 特性门禁）和上一讲的 `HttpServer` 一样，是「周边工具型」积木——它**不参与编译、不实现 `World` trait**，只负责「盯着磁盘、发现变化、唤醒重编译」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-kit/src/watcher.rs` | 本讲主角。定义 `Watcher`，封装 notify-rs，实现标记-清除式增量监听、事件批处理、missing 轮询、inotify 重 watch 修复。 |
| `crates/typst-cli/src/watch.rs` | watcher 的「标准用法」。`watch()` 函数把 watcher 接入「update → wait → reset → recompile」的主循环。 |
| `crates/typst-kit/src/files.rs` | 提供 `FileStore::dependencies()`（产出 `FileId`）与 `SystemFiles::resolve()`（把 `FileId` 还原成磁盘 `PathBuf`），是 watcher 获取待监听路径的数据来源。 |

依赖关系链路（务必先建立这条全局画面）：

```
  SystemWorld::dependencies()            (typst-cli/src/world.rs)
            │  内部调用
            ▼
  FileStore::dependencies()              (files.rs) → 返回 (loader, FileId 迭代器)
            │  filter_map(|id| loader.resolve(id))
            ▼
  Iterator<Item = PathBuf>               ← 把 FileId 还原成真实磁盘路径
            │  喂给
            ▼
  Watcher::update(world.dependencies())  (watcher.rs)
            ▼
  Watcher::wait()                        阻塞，直到磁盘有变化
```

## 4. 核心概念与源码讲解

### 4.1 Watcher 的定位与数据结构

#### 4.1.1 概念说明

`typst watch` 的核心循环其实非常朴素：

```
编译一次 → 盯着这次编译读过的所有文件 → 文件一变就重新编译 → 再盯着新的依赖集合 → ……
```

注意一个关键点：**「要盯着哪些文件」不是固定的，而是由上一次编译动态决定的。** 假设你这次新写了一行 `#include "chapter2.typ"`，那么 `chapter2.typ` 就成了新的依赖，下一轮就必须开始监视它；反过来，如果你删掉了这行 `#include`，`chapter2.typ` 就不再是依赖，下一轮就应该停止监视它，免得它的无关变化一次次触发重编译。

`Watcher` 这个类型，就是负责「把一组路径收下、记住、并交给 notify-rs 去盯着」的那个管理者。它要解决的三个问题：

1. **新增依赖**：新出现的路径要开始 watch。
2. **过期依赖**：不再需要的路径要 unwatch（否则会持续收到无关事件、浪费资源）。
3. **不存在的文件**：有些依赖路径可能此刻根本不存在（比如你 `#include` 了一个还没建的文件），notify-rs 没法监视不存在的文件，需要单独处理。

#### 4.1.2 核心流程

`Watcher` 内部维护四样东西：

- 一个 notify-rs 的 `RecommendedWatcher`（真正盯盘的家伙）。
- 一个接收事件的 channel `rx`（notify-rs 把事件往这里塞）。
- 一个 `watched: FxHashMap<PathBuf, bool>`：**当前正在监视的路径集合**，其中的布尔值是「标记-清除」用的存活标记。
- 一个 `missing: FxHashSet<PathBuf>`：**想监视但文件还不存在、只能轮询的路径集合**。

`new()` 时这些容器都为空；真正「开始监视」是在每次 `update()` 里完成的。

#### 4.1.3 源码精读

整个模块用 `#![cfg(feature = "watcher")]` 在文件级门禁——不开 `watcher` 特性时，这个模块根本不参与编译（参见 [src/watcher.rs:1-5](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L1-L5)）。该特性只引入两个额外依赖：`notify` 和 `same-file`（见 [Cargo.toml:81-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L81-L82)）。

`Watcher` 的字段定义在 [src/watcher.rs:19-33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L19-L33)，字段注释里直接点明了布尔值的作用（下文 4.2 详解）：

```rust
pub struct Watcher {
    /// The output file. We ignore any events for it.
    output: Option<PathBuf>,
    /// The underlying watcher.
    watcher: RecommendedWatcher,
    /// Notify event receiver.
    rx: Receiver<notify::Result<Event>>,
    /// Keeps track of which paths are watched via `watcher`. The boolean is
    /// used during updating for mark-and-sweep garbage collection of paths we
    /// should unwatch.
    watched: FxHashMap<PathBuf, bool>,
    /// A set of files that should be watched, but don't exist. We manually poll
    /// for those.
    missing: FxHashSet<PathBuf>,
}
```

构造函数 `new()` 见 [src/watcher.rs:50-70](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L50-L70)。它做了三件事：

```rust
let (tx, rx) = std::sync::mpsc::channel();
// 把 notify 默认的轮询间隔调小（默认约 30s 太久，这里 300ms）
let config = notify::Config::default().with_poll_interval(Self::POLL_INTERVAL);
let watcher = RecommendedWatcher::new(tx, config).map_err(...)?;
Ok(Self { output, rx, watcher, watched: FxHashMap::default(), missing: FxHashSet::default() })
```

要点：

- notify 用「回调转 channel」模式工作：构造时传入 `tx`，notify 在后台线程收到事件后 `tx.send(event)`，这边通过 `rx` 取出。于是 `Watcher` 把一个异步的监听器，适配成了「我可以随时去 `rx` 里阻塞地取事件」的同步接口。
- `with_poll_interval` 注释里特别说明：这个值只影响极少数使用 `PollWatcher` 后端的系统（大部分系统用的是事件驱动的 inotify/FSEvents，不受影响），但一旦命中，30s 的默认值会让 `typst watch` 显得迟钝，故调小。这是典型的「针对边缘情况做防御性调参」。
- `output` 字段记录「输出文件路径」。因为 `typst watch` 会把编译产物写回磁盘，而这次写盘本身会触发文件事件——如果不忽略它，就会陷入「编译→写产物→触发事件→又编译」的死循环。后面 `wait()` 里会用到它。

#### 4.1.4 代码实践

**目标**：确认 watcher 模块的门禁与依赖，建立「按需启用」的直觉。

1. 在本仓库根目录查看 `crates/typst-kit/Cargo.toml` 的 `watcher` 特性定义（[Cargo.toml:81-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/Cargo.toml#L81-L82)）。
2. 想象你要在一个只依赖 `typst-kit`、且只开了 `embedded-fonts` 的项目里 `use typst_kit::watcher::Watcher;`。

**需要观察的现象**：因为 watcher 整个模块被 `#![cfg(feature = "watcher")]` 门禁（[src/watcher.rs:5](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L5)），不开该特性时 `Watcher` 类型不存在，编译会报 `unresolved module / cannot find type`。

**预期结果**：开启 `watcher` 特性后编译通过；同时由于 `watcher = ["dep:notify", "dep:same-file"]`，会自动引入这两个 crate。

> 待本地验证：实际跑一次 `cargo check`（分别带与不带 `--features typst-kit/watcher`）以观察报错差异。

#### 4.1.5 小练习与答案

**练习 1**：watcher 模块是用 `#![cfg(feature = "watcher")]` 整模块门禁，还是用 `#[cfg]` 逐条目门禁？为什么这样选？

**参考答案**：整模块门禁。因为 `watcher.rs` 里**没有**任何「不开特性也要常驻」的代码——整个文件的存在只为文件监听服务，所以可以用文件级的 `#![cfg]` 把它整体关掉，比逐条标注更干净。（对照 fonts/files/packages 等模块，它们有「常驻代码 + feature 条目」，所以只能用条目级 `#[cfg]`。）

**练习 2**：`new()` 里为什么要调 `with_poll_interval(Self::POLL_INTERVAL)`？

**参考答案**：notify 的 `PollWatcher` 后端默认轮询间隔约 30s，对 `typst watch` 来说太慢。把这个间隔调小到 300ms，让少数使用轮询后端的系统也能较及时地感知变化。注意它只影响 `PollWatcher`，绝大多数平台用的是事件驱动后端，不受影响。

---

### 4.2 update()：标记-清除式增量 watch / unwatch

#### 4.2.1 概念说明

`update()` 是 watcher 的「收件箱」：调用方把「这一轮要监视的所有路径」一次性倒进来，它负责把 watcher 的实际监听集合**收敛到**这个新集合。

难点在于「增量」二字。最朴素的实现是「每次全部 unwatch、再全部 watch」，但它有两个问题：

1. 性能差，对大量文件反复增删 watch 句柄很浪费。
2. 更要命的是——某些编辑器保存文件时，会做「删旧文件 → 把临时文件改名过去」这套动作（见 4.5）。频繁 unwatch/rewatch 会让窗口期变长。

于是 typst-kit 选择了**标记-清除**：只对「真正变化了」的路径做一次 watch / unwatch，没变的路径完全不动。

#### 4.2.2 核心流程

`update(iter)` 三步走（伪代码）：

```
1. 【清除阶段·复位标记】
   for 每个已 watched 路径:
       watched[path] = false          # 先假定它「本轮可能要被清掉」

2. 【标记阶段·登记本轮依赖】
   self.missing.clear()               # 重新统计 missing
   for path in iter (本轮依赖):
       if path 不存在:
           missing.insert(path)        # 交给轮询机制，不进 notify
           continue
       if path 不在 watched 里:        # 是新依赖 → 真正调用 notify 开始监视
           watcher.watch(path, NonRecursive)?
       watched[path] = true            # 标记为「存活」

3. 【清除阶段·清扫】
   watched.retain(|path, seen| {
       if not seen:                    # 本轮没被标记 → 不再依赖
           watcher.unwatch(path)       # 停止监视
       seen                            # 只保留 seen=true 的
   })
```

注意第二步里 `watched.insert(path, true)` 对「已存在」的路径只是把标记从 `false` 改回 `true`，**不会**再次 `watch()`——这是增量优化的关键：只有 `!self.watched.contains_key(&path)`（全新路径）才会真正调用 notify 的 `watch`。

#### 4.2.3 源码精读

`update()` 全文见 [src/watcher.rs:76-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L76-L118)。逐段看：

**复位标记**（[src/watcher.rs:79-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L79-L82)）：

```rust
#[expect(clippy::iter_over_hash_type, reason = "order does not matter")]
for seen in self.watched.values_mut() {
    *seen = false;
}
```

把 `watched` 里每个路径的布尔值都翻成 `false`。这相当于标记-清除的「先全部假定是垃圾」。注释里的 `#[expect]` 是因为遍历哈希表的顺序不确定，但这里**顺序无关紧要**，所以显式豁免 lint。

**标记本轮依赖**（[src/watcher.rs:84-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L84-L107)）：

```rust
// Reset which files are missing.
self.missing.clear();

for path in iter {
    // 不存在的文件 notify 监视不了，先记进 missing 集合
    if !path.exists() {
        self.missing.insert(path);
        continue;
    }
    // 全新路径才真正调用 notify.watch
    if !self.watched.contains_key(&path) {
        self.watcher
            .watch(&path, RecursiveMode::NonRecursive)
            .map_err(|err| eco_format!("failed to watch {path:?} ({err})"))?;
    }
    // 标记为存活（无论是否新路径）
    self.watched.insert(path, true);
}
```

两个要点：

- `RecursiveMode::NonRecursive`：只监视这个路径本身，**不递归**进它的子目录。Typst 的依赖是「一个一个的具体文件」，不需要整目录递归。
- missing 判定用 `path.exists()`——稍后 4.5 详述。

**清扫**（[src/watcher.rs:109-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L109-L115)）：

```rust
self.watched.retain(|path, &mut seen| {
    if !seen {
        self.watcher.unwatch(path).ok();
    }
    seen
});
```

`HashMap::retain` 会**保留**返回 `true` 的项、删掉返回 `false` 的项。这里：`seen == false`（本轮没被重新标记）→ 先 `unwatch` 再删；`seen == true` → 保留。`unwatch` 用 `.ok()` 吞掉错误——即使 notify 报「我没在监视这个路径」也不影响整体逻辑。

**调用方如何喂数据**：`update()` 接收的是 `PathBuf`，但 FileStore 给出的是 `FileId`。中间的「还原」发生在 typst-cli 的 `SystemWorld::dependencies()`（[crates/typst-cli/src/world.rs:97-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L97-L101)）：

```rust
pub fn dependencies(&mut self) -> impl Iterator<Item = PathBuf> + '_ {
    let (loader, deps) = self.files.dependencies();
    deps.filter_map(|id| loader.resolve(id).ok())
}
```

其中 `loader.resolve(id)` 在 `SystemFiles` 上的实现是 [src/files.rs:304-306](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L304-L306)，它把虚拟的 `FileId`（项目根 / 包根 + 虚拟路径）映射回真实的磁盘路径。于是 watcher 拿到的始终是「磁盘上看得见摸得着」的 `PathBuf`。

#### 4.2.4 代码实践

**目标**：亲手推演标记-清除中 `watched` 布尔值的三态变化。

操作步骤（纯源码阅读 + 纸笔推演，无需运行）：

1. 假设第一轮编译依赖 `{a.typ, b.typ}`，两个文件都存在。读 [src/watcher.rs:76-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L76-L118)，写出第一轮 `update` 后 `watched` 的状态。
2. 假设第二轮编译依赖变成 `{b.typ, c.typ}`（去掉了 `a.typ`，新增了 `c.typ`），再写出第二轮三个阶段中 `watched` 的状态变化。

**预期结果**：

| 时机 | `watched` 内容 | 说明 |
| --- | --- | --- |
| 第一轮结束后 | `{a.typ: true, b.typ: true}` | 两个都新 watch |
| 第二轮·复位后 | `{a.typ: false, b.typ: false}` | 全部翻 false |
| 第二轮·标记后 | `{a.typ: false, b.typ: true, c.typ: true}` | b 复标，c 新增 watch |
| 第二轮·清扫后 | `{b.typ: true, c.typ: true}` | `a.typ` 因 false 被 unwatch 并删除 |

注意 `a.typ` 在第二轮里**从未被 `contains_key` 命中**，所以它从始至终不会被再次 `watch()`，只会在清扫时被 `unwatch`——这正是「不动没变的」的体现。

#### 4.2.5 小练习与答案

**练习 1**：如果第二轮依赖和第一轮完全一样，`update` 会调用几次 notify 的 `watch` / `unwatch`？

**参考答案**：各 0 次。复位阶段把标记翻 false，标记阶段对每个路径都 `contains_key` 命中（已存在），跳过 `watch`，只把标记翻回 true；清扫阶段所有项 `seen == true`，全部保留、不 `unwatch`。所以「依赖没变」时 `update` 对 notify 是零开销的——这正是标记-清除增量的价值。

**练习 2**：为什么清扫里 `unwatch` 要用 `.ok()` 吞掉错误？

**参考答案**：要清扫的路径可能已经因为别的原因（比如 4.5 的 inotify 隐式 unwatch，或文件已被删除）不在 notify 的监视列表里了，此时 `unwatch` 会返回错误。但这不是真正的故障——我们的目标就是「让它不被监视」，既然已经不被监视了，忽略这个错误即可，不应让整个 `update` 失败。

---

### 4.3 is_relevant_event_kind：哪些事件值得重编译

#### 4.3.1 概念说明

notify-rs 对一次文件操作可能发出**多个**事件，而且并非每个都意味着「源码变了」。例如：

- 仅仅是**访问（Access）**文件（比如读取它的元数据）——内容没变，不该重编译。
- 仅仅是**元数据（Metadata）**变化（比如改了文件的修改时间但没改内容）——通常也不该重编译。
- 文件被**创建 / 删除 / 内容被改 / 重命名**——这才是真正可能影响编译结果的事件。

`is_relevant_event_kind` 就是一个把 notify 的 `EventKind` 翻译成「要不要在意」布尔值的过滤器，避免无谓的重编译。

#### 4.3.2 核心流程

```
is_relevant_event_kind(kind):
    Any              → true   # 不明事件，保守起见算相关
    Access(_)        → false  # 纯访问，忽略
    Create(_)        → true   # 新建
    Modify:
        Any          → true
        Data(_)      → true   # 内容（数据）被改
        Metadata(_)  → false  # 仅元数据改
        Name(_)      → true   # 改名（创建/删除的别名）
        Other        → false
    Remove(_)        → true   # 删除
    Other            → false
```

#### 4.3.3 源码精读

全文见 [src/watcher.rs:196-211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L196-L211)：

```rust
fn is_relevant_event_kind(kind: notify::EventKind) -> bool {
    match kind {
        notify::EventKind::Any => true,
        notify::EventKind::Access(_) => false,
        notify::EventKind::Create(_) => true,
        notify::EventKind::Modify(kind) => match kind {
            notify::event::ModifyKind::Any => true,
            notify::event::ModifyKind::Data(_) => true,
            notify::event::ModifyKind::Metadata(_) => false,
            notify::event::ModifyKind::Name(_) => true,
            notify::event::ModifyKind::Other => false,
        },
        notify::EventKind::Remove(_) => true,
        notify::EventKind::Other => false,
    }
}
```

设计要点：

- **对不确定的事件保守地返回 `true`**（`Any`、`Modify::Any`）。宁可多编一次，也不要漏掉真正的改动——漏编会让 `watch` 失去意义。
- **明确无意义的返回 `false`**（`Access`、`Modify::Metadata`）。这两类是「文件内容并未改变」的典型，过滤掉能显著减少无谓重编译。

#### 4.3.4 代码实践

**目标**：理解过滤的取舍。

操作步骤：

1. 阅读上述 [src/watcher.rs:196-211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L196-L211)。
2. 思考：假如把 `ModifyKind::Metadata(_) => false` 改成 `=> true`，会发生什么？

**预期结果**：那么仅仅 `touch` 一下文件（更新 mtime 但不改内容）就会触发重编译，造成很多「什么都没改却重编了」的浪费。这正是把它过滤为 `false` 的理由。反过来，若把某个 `true` 改成 `false`，则有「源码改了却不重编」的风险。所以这份 match 的每一个分支都是在「灵敏度」和「效率」之间做的权衡。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `EventKind::Any` 和 `ModifyKind::Any` 都返回 `true`？

**参考答案**：`Any` 表示「notify 无法进一步分类」。既然无法判断，就按「最坏情况」处理——假定它相关。对 watch 而言，多编一次的代价远小于「漏编导致结果陈旧」的代价，所以不确定时一律算相关。

**练习 2**：用 `#image("logo.png")` 引入的图片被「内容替换」时，对应的事件会被判为相关吗？

**参考答案**：会。替换图片内容通常产生 `Modify(Data(_))` 事件，按上面 `Data(_) => true` 会被判为相关，从而触发重编译——这正是期望的行为（图片变了，输出也该更新）。

---

### 4.4 wait()：事件批处理与超时

#### 4.4.1 概念说明

`wait()` 负责阻塞，直到「真正需要重编译的变化」出现。它要解决一个棘手的现实问题：**很多编辑器保存文件时不是「原地改一下」那么简单**，而是一连串动作。例如常见的「原子保存」流程是：

```
把内容写到临时文件 tmp.xxx → 删除原文件 → 把 tmp.xxx 重命名为原文件名
```

在 notify 看来，这会接连冒出 `Remove(原文件)` → `Create(tmp)` → `Modify(Name, From=tmp)` → `Modify(Name, To=原文件)` 等好几个事件，且彼此间隔可能只有几毫秒。如果 watcher 看到一个事件就立刻重编译，那一次保存会触发**好几次**编译，既浪费又可能编译到「半成品状态」（比如文件正被改名到一半）。

解决办法是**批处理（batching）**：收到第一个事件后，再等一小段时间，把这一小段内涌入的所有事件「攒成一批」，只在批处理结束后重编译一次。

但批处理又不能无脑等下去——如果磁盘持续不断地变化（事件风暴），一直攒下去就永远不重编译了。所以还需要一个**「饿了就停」（starve）**上限：攒够一定时间，就算还有事件也必须开始编译。

#### 4.4.2 核心流程

`wait()` 主循环（伪代码）：

```
loop:
    # ① 等第一个事件
    first = rx.recv_timeout( 无 missing ? Duration::MAX : POLL_INTERVAL )

    # ② 批处理：把涌入的事件攒一波
    relevant = false
    batch_start = now()
    for event in [first] ++ 流式追加(每个 BATCH_TIMEOUT 内到来的事件):
        if 超过 STARVE_TIMEOUT 自 batch_start: 停止追加   # 饿了就停

        event = 解包 channel 错误?
        if 不是相关事件: continue
        if 是 Remove(File) 或 Rename(From):  # inotify 修复，见 4.5
            对每个 path: watcher.unwatch(path); watched.remove(path)
        if 事件全是输出文件: continue        # 忽略产物
        relevant = true

    # ③ 本批若相关，或某个 missing 文件出现了，就返回触发重编译
    if relevant or missing 中有文件已存在:
        return Ok(())
    # 否则继续 loop（通常发生在「收到的都是输出文件 / 不相关事件」时）
```

三个超时常量（[src/watcher.rs:37-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L37-L45)）：

| 常量 | 值 | 作用 |
| --- | --- | --- |
| `BATCH_TIMEOUT` | 100ms | 收到一个事件后，最多再等这么久看有没有紧跟的事件；超时则认为「这一批攒完了」。 |
| `STARVE_TIMEOUT` | 500ms | 批处理总时长上限；攒到这么久就算还有事件也必须开编，防止事件风暴饿死编译。 |
| `POLL_INTERVAL` | 300ms | 有 missing 文件时，等第一个事件的轮询间隔（见 4.5）。 |

#### 4.4.3 源码精读

`wait()` 全文见 [src/watcher.rs:121-192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L121-L192)。关键片段：

**① 等第一个事件**（[src/watcher.rs:126-130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L126-L130)）：

```rust
let first = self.rx.recv_timeout(if self.missing.is_empty() {
    Duration::MAX
} else {
    Self::POLL_INTERVAL
});
```

如果没有 missing 文件，就无限期等（`Duration::MAX`）；如果有 missing 文件，最多等 `POLL_INTERVAL`，到点就「假装收到一个超时」去触发 missing 检查（见 4.5）。

**② 批处理迭代器**（[src/watcher.rs:139-144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L139-L144)）是本讲最精巧的一段：

```rust
let mut relevant = false;
let batch_start = Instant::now();
for event in first
    .into_iter()
    .chain(iter::from_fn(|| self.rx.recv_timeout(Self::BATCH_TIMEOUT).ok()))
    .take_while(|_| batch_start.elapsed() <= Self::STARVE_TIMEOUT)
{
```

逐层拆解这个迭代器：

- `first.into_iter()`：把第一个事件（一个 `Result`）变成「至多产出一个元素」的迭代器。若第一步超时（`first` 是 `Err`），这一段什么都不产出，循环体不执行，`relevant` 保持 false，直接跳到第③步去检查 missing。
- `.chain(iter::from_fn(closure))`：这是一个**按需、无限**的迭代器。每当代码向它要下一个元素，它就执行 `closure`：调 `rx.recv_timeout(BATCH_TIMEOUT)`——
    - 若 100ms 内来了下一个事件 → 返回 `Some(...)`，迭代器继续产出，循环继续；
    - 若 100ms 内没事件（超时） → `.ok()` 得到 `None`，迭代器**就此停止**，攒批结束。
- `.take_while(|_| batch_start.elapsed() <= Self::STARVE_TIMEOUT)`：无论上面怎么产出，只要从批开始已经过了 500ms，就强制截断。这是防饿死保险。

> 这就是「用迭代器组合来表达『攒到安静 / 攒到上限』」的优雅写法：`chain + from_fn` 表达「持续追加直到安静」，`take_while` 表达「最多攒这么久」。

**事件处理**（[src/watcher.rs:145-184](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L145-L184)），三道关：

1. 先 `event.map_err(...)?` 解包 channel 层的 notify 错误（罕见的 watcher 内部错误会直接从 `wait` 往外抛）。
2. 过滤无关事件（4.3）：`if !is_relevant_event_kind(event.kind) { continue; }`。
3. inotify 修复（见 4.5）。
4. 忽略输出文件（[src/watcher.rs:174-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L174-L181)）：

```rust
if let Some(output) = &self.output
    && event.paths.iter().all(|path| is_same_file(path, output).unwrap_or(false))
{
    continue;
}
```

注意这里用的是 `is_same_file`（来自 `same-file` crate）而非简单的路径相等。因为「原子保存」会把原文件替换成新 inode，产物文件可能改名临时文件而来——用「是否同一个文件实体」来判断比路径相等更稳。只有当本事件的**所有**路径都是输出文件时才忽略（`all`），部分相关时仍算相关。

通过这三关后，`relevant = true`。

**③ 收尾**（[src/watcher.rs:187-190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L187-L190)）：

```rust
if relevant || self.missing.iter().any(|path| path.exists()) {
    return Ok(());
}
```

本批里有相关事件、或某个 missing 文件此刻出现了，就返回触发重编译；否则回到循环顶部继续等（典型场景：收到的全是输出文件事件，或全是无关事件）。

**主循环的拼装**：以上 `update` / `wait` 如何被串进 `typst watch`，见 [crates/typst-cli/src/watch.rs:67-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L67-L83)：

```rust
loop {
    watcher.update(world.dependencies())?;   // 用本轮依赖更新监视集合
    watcher.wait()?;                          // 阻塞到有相关变化
    world.reset();                            // 清掉依赖记录、准备重编
    timer.record(&mut world, |world| compile_once(world, &mut config))??;
    comemo::evict(10);
}
```

注意顺序：**先 update 再 wait**。这意味着每次重编后，会立刻用「最新依赖」刷新监视集合，然后再阻塞等待——保证下一次醒来时盯的永远是最新依赖。

#### 4.4.4 代码实践

**目标**：推演「原子保存」一串事件在一次 `wait` 里的批处理过程。

操作步骤（纸笔推演）：

1. 设想编辑器在 `t=0` 触发 `Remove(原文件)`，`t=2ms` 触发 `Create(tmp)`，`t=5ms` 触发 `Modify(Name, To=原文件)`，之后 100ms 内无新事件。
2. 对照 [src/watcher.rs:139-184](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L139-L184)，描述这一批事件如何被一次性消费。

**预期结果**：

- `first` 拿到 `Remove` 事件（`t=0`），`batch_start` 记下当前时刻，`relevant` 经 inotify 修复分支处理后（见 4.5）最终置为 `true`。
- `chain` 继续取：`t=2ms` 的 `Create`、`t=5ms` 的 `Modify` 都在各自的上一个事件之后 100ms 内到来，全部被纳入同一批，`relevant` 保持 `true`。
- `t=5ms` 之后再 `recv_timeout(100ms)`，到 `t≈105ms` 超时返回 `None`，迭代器停止，批处理结束。
- 整批只产生**一次** `return Ok(())` → 只触发**一次**重编译。三个事件被「攒平」成一个编译动作。

需要观察的现象：编辑器越「花哨」的保存方式（删 + 改名越多），批处理省下的重复编译就越多。

#### 4.4.5 小练习与答案

**练习 1**：把 `STARVE_TIMEOUT` 调到无穷大（去掉 `take_while`）会有什么后果？

**参考答案**：若磁盘持续产生事件（事件风暴，例如某个程序不停地写日志文件恰好被监视），批处理永远不会因为「安静」而结束——因为总有事件在 100ms 内到来，迭代器永不停止，`wait` 永不返回，重编译被永久饿死。`STARVE_TIMEOUT` 就是兜底保证「最多攒 500ms 就必须开编」。

**练习 2**：`first.into_iter()` 当 `first` 是超时错误时会产出什么？

**参考答案**：什么都不产出（空迭代器）。于是 `chain` 的第一段是空的，但 `chain` 仍会继续从 `from_fn` 取——也就是说即便第一个 `recv_timeout` 超时了，批处理仍可能紧接着取到刚到的事件。不过更常见的路径是：超时发生在「没有 missing」时是不可能的（用了 `Duration::MAX`），只有在「有 missing」时才可能超时，此时循环体很可能不执行，直接走到第③步检查 missing。

---

### 4.5 missing 轮询与 inotify 移除/重命名修复

#### 4.5.1 概念说明

本模块讲两个「兜底」机制，它们都体现「真实世界比理想模型更脏」：

**机制一：missing 轮询。** notify-rs **无法监视一个还不存在的文件**——你得先有一个文件，notify 才能盯着它。但 Typst 允许 `#include "chapter-not-yet-written.typ"`，这时这个文件还不存在，却确实是依赖。怎么办？typst-kit 把这类路径收进 `missing` 集合，然后**定时轮询**它们是否被创建出来。一旦创建，`wait()` 就返回触发重编译（重编后这个文件已存在，`update` 就能用 notify 正常监视它了）。

**机制二：inotify 的隐式 unwatch。** Linux 的 inotify 有个讨厌的特性：当你监视的文件被**删除**或**重命名**（注意是「被监视文件本身被删/改名」，不是它的内容变）时，inotify 会**自动且静默地**取消对这个路径的监视——你不会再收到关于它的任何后续事件。这恰好是「原子保存」的高频场景。如果不补救，保存一次文件后这个文件就「失联」了，再编辑也不会触发重编译。typst-kit 的对策：识别出这类事件后，主动把这个路径从 `watched` 里抹掉，这样下一轮 `update` 会发现它「不在监视列表里」，于是重新 `watch()`，重新建立连接。

#### 4.5.2 核心流程

**missing 轮询**贯穿 `update` 与 `wait`：

```
update 阶段:
    missing.clear()
    for path in 依赖:
        if !path.exists(): missing.insert(path)   # 不存在的进轮询池

wait 阶段:
    recv 超时取 POLL_INTERVAL（而非 MAX）当且仅当 missing 非空
    每轮结束时: if missing.iter().any(|p| p.exists()): 返回触发重编译
```

**inotify 修复**发生在 `wait` 的事件处理里：

```
for event in 批:
    if event.kind 是 Remove(File) 或 Modify(Name, RenameMode::From):
        for path in event.paths:
            watcher.unwatch(path)   # 显式清理（notify 已隐式 unwatch 了）
            watched.remove(path)    # 从 watched 抹掉 → 下轮 update 会重新 watch
```

为什么是 `Remove(File)` 和 `Modify(Name, RenameMode::From)` 这两种？

- `Remove(File)`：被监视文件被删除。
- `Modify(Name, RenameMode::From)`：被监视文件被改名（「改名」在 notify 里是一对事件：`From` 表示「旧名字消失」，`To` 表示「新名字出现」）。旧名字消失等同于这个路径上的文件没了，inotify 会取消监视。

二者都是「这个路径上的文件没了」，所以都触发修复。

#### 4.5.3 源码精读

**missing 的写入**在 `update`（已在 4.2 引用，[src/watcher.rs:93-96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L93-L96)）：

```rust
if !path.exists() {
    self.missing.insert(path);
    continue;
}
```

**missing 如何缩短 `wait` 的等待**（[src/watcher.rs:126-130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L126-L130)）：当 `missing` 非空时，`recv_timeout` 用 `POLL_INTERVAL` 而非 `Duration::MAX`。这样即使没有任何 notify 事件，每 300ms 也会「醒来」一次。

**醒来后检查 missing**（[src/watcher.rs:188-189](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L188-L189)）：

```rust
if relevant || self.missing.iter().any(|path| path.exists()) {
    return Ok(());
}
```

某个 missing 文件已存在 → 触发重编译。重编后该文件已是真实依赖，下一轮 `update` 中 `path.exists()` 为真，于是从 `missing` 移出、进入 `watched` 正式监视。

> missing 轮询在 typst-cli 里的「真实用武之地」是启动阶段：当输入文件还不存在时，`watch()` 会用 `watcher.update([path])` + `watcher.wait()` 等它被创建，见 [crates/typst-cli/src/watch.rs:42-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L42-L45)。此时该路径在 `update` 里进入 `missing`，`wait` 靠轮询等它出现。

**inotify 修复**（[src/watcher.rs:152-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L152-L170)）：

```rust
// Workaround for notify-rs' implicit unwatch on remove/rename
// (triggered by some editors when saving files) with the inotify backend.
if matches!(
    event.kind,
    notify::EventKind::Remove(notify::event::RemoveKind::File)
        | notify::EventKind::Modify(notify::event::ModifyKind::Name(
            notify::event::RenameMode::From
        ))
) {
    for path in &event.paths {
        self.watcher.unwatch(path).ok();
        self.watched.remove(path);
    }
}
```

注释把动机说得很清楚：这是针对「部分编辑器保存文件」触发 remove/rename、且在 inotify 后端下会隐式 unwatch 的 workaround。做法是「把这个路径从 `watched` 里删掉」。注意这里**不立刻重新 watch**——因为这一批事件还没处理完，文件可能正处在「删了一半」的瞬态。真正的重新 watch 推迟到**下一轮 `update`**：那时 `watched` 里已没有这个路径，`!self.watched.contains_key(&path)` 成立，于是重新 `watcher.watch()`。这就是为什么 `wait` 里要把这类事件 `relevant = true`——它必须触发一次重编译，从而触发一次 `update`，从而完成重新 watch。

#### 4.5.4 代码实践

**目标**：把本讲的两个难点串起来，理解「保存一个被监视文件」的完整往返。

操作步骤（纸笔推演一个 Linux + inotify + 原子保存编辑器的场景）：

1. 设 `a.typ` 当前在 `watched` 里（`{a.typ: true}`）。
2. 编辑器执行原子保存：`Remove(a.typ)` → 写 tmp → `Modify(Name, To=a.typ)`。注意「`a.typ` 这个名字上的原文件被删」对应 `Remove(File)`。
3. 对照 [src/watcher.rs:152-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L152-L170) 与 [src/watcher.rs:76-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L76-L118)，描述 `a.typ` 从「被删」到「重新被监视」的全过程。

**预期结果**：

| 阶段 | `watched` 中 `a.typ` 状态 | 说明 |
| --- | --- | --- |
| 保存前 | `true` | 正常监视 |
| `wait` 收到 `Remove(File)` | 被从 `watched` 删除 | inotify 已隐式 unwatch；watcher 显式 unwatch + remove，留待下轮重建 |
| `wait` 返回 | — | `relevant = true`，触发重编译 |
| 重编译后 `update` 再次跑 | 重新 `watch()` 并置 `true` | 因为 `a.typ` 不在 `watched`，命中「全新路径」分支，重新建立监视 |

若**没有**这个修复：`Remove(File)` 后 inotify 静默取消监视，`watched` 仍以为 `a.typ: true`，下一轮 `update` 时 `contains_key` 命中、**跳过** `watch()`，于是 `a.typ` 永远失联——之后怎么改都不再触发重编译。这正是这段 workaround 存在的全部理由。

> 待本地验证：在有 inotify 的 Linux 上用 `vim`（默认 atomic save）跑 `typst watch`，连续保存两次，确认每次都能触发重编译（说明重新 watch 生效）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 inotify 修复里只匹配 `RenameMode::From` 而不匹配 `RenameMode::To`？

**参考答案**：`From` 表示「旧路径上的文件消失了」（被监视文件被改名搬走），这正是 inotify 会隐式 unwatch 的情形，需要补救。`To` 表示「某个新路径上出现了文件」（新名字出现），它对应的是创建，并不会让任何**已被监视的路径**失联，不需要从 `watched` 里删除。所以只需对 `From` 做清理。

**练习 2**：missing 集合里的文件被创建后，watcher 是怎么把它「转正」成 notify 正常监视的？

**参考答案**：文件创建后被 `wait` 的 missing 检查发现（`missing.iter().any(|p| p.exists())`），`wait` 返回触发重编译。重编译里这个文件被实际读取，成为新的真实依赖。下一轮 `update` 遍历依赖时，`path.exists()` 已为真，于是它不再进 `missing`，而是因为「不在 `watched`」走 `watcher.watch()` 分支，正式进入 `watched` 由 notify 监视。从此它就和其他正常依赖一样工作了。

---

## 5. 综合实践

**任务**：用一张「事件→状态→动作」的时序图，把 `typst watch` 的一次完整「保存→重编译」讲清楚，覆盖本讲全部要点。

场景设定（Linux + inotify + 原子保存编辑器）：当前依赖 `{main.typ, chapter.typ}`（均存在并已被监视），输出文件为 `out.pdf`。用户保存 `chapter.typ`。

请按下面的骨架填写每一格，并在括号里标注对应源码位置：

1. **保存前**：`watched = {main.typ: true, chapter.typ: true}`，`missing = {}`，watcher 阻塞在 `wait()` 的 `recv_timeout(Duration::MAX)`（因 missing 为空）。
2. **编辑器写 tmp**：notify 发 `Create(tmp)` 事件 → 进入批处理（[src/watcher.rs:140-144](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L140-L144)）。判断：tmp 不是输出文件、是 `Create` → 相关，`relevant = true`。
3. **编辑器删 chapter.typ**：notify 发 `Remove(File)` 命中 inotify 修复分支（[src/watcher.rs:157-170](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L157-L170)）→ `watcher.unwatch(chapter.typ)`、`watched.remove(chapter.typ)`。`relevant = true`。
4. **编辑器把 tmp 改名为 chapter.typ**：notify 发 `Modify(Name, To)`。`To` 不触发修复，但属于相关事件，`relevant = true`。
5. **攒批结束**：100ms 内无新事件，迭代器停止（[src/watcher.rs:142](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L142)）。`relevant == true` → `wait` 返回（[src/watcher.rs:188](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L188)）。注意：三个事件只产生**一次**返回。
6. **主循环**（[crates/typst-cli/src/watch.rs:68-83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs#L68-L83)）：`world.reset()` → 重编译 → 编译会重新读 `chapter.typ`，依赖集合仍是 `{main.typ, chapter.typ}`。
7. **下一轮 update**（[src/watcher.rs:76-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L76-L118)）：复位标记 → 遍历依赖。`chapter.typ` 此刻 `exists()` 为真且**不在 `watched`**（第 3 步删掉了）→ 命中 `!contains_key` 分支，重新 `watcher.watch()`，重新置 `true`。`main.typ` 则因为仍在 `watched`、只把标记从 false 翻回 true，不重新 watch。清扫阶段无项被删。

**最终检查**：完成后 `chapter.typ` 重新回到 `watched` 且 `true`——失联的监视被悄悄重建，用户毫无感知。这正是一个健壮的 watcher 该有的样子。

> 进阶可选：若你本地有 Rust 环境，开启 `watcher` 特性，写一个最小程序：`Watcher::new(None)` → `update([某测试文件])` → 在另一终端 `echo >> 该文件` → 调 `wait()` 看它是否如期返回。观察完后关掉。（待本地验证。）

## 6. 本讲小结

- `Watcher`（[src/watcher.rs:19-33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L19-L33)）是「周边工具型」积木，不参与编译；它把 notify-rs 的异步监听适配成「`update` 收路径 / `wait` 取变化」的同步接口，服务于 `typst watch`。
- `update()`（[src/watcher.rs:76-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L76-L118)）用**标记-清除**做增量 watch/unwatch：`watched` 的布尔值先全复位 false、再按本轮依赖标 true、最后清扫仍为 false 的。依赖没变时对 notify 零开销。
- `is_relevant_event_kind`（[src/watcher.rs:196-211](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L196-L211)）过滤掉 Access/Metadata 这类「内容没真变」的事件，不确定的事件保守算相关。
- `wait()`（[src/watcher.rs:121-192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/watcher.rs#L121-L192)）用 `chain(from_fn) + take_while` 把一串事件攒成一批：`BATCH_TIMEOUT`(100ms) 决定「攒到安静」、`STARVE_TIMEOUT`(500ms) 防止事件风暴饿死编译，并用 `is_same_file` 忽略输出文件事件。
- **missing 轮询**：notify 监视不了不存在的文件，故把它们收进 `missing`，`wait` 改用 `POLL_INTERVAL`(300ms) 超时定期检查它们是否出现。
- **inotify 修复**：被监视文件被删/改名时 inotify 会隐式 unwatch；watcher 在 `Remove(File)` / `Modify(Name, From)` 时把路径从 `watched` 抹掉，下一轮 `update` 自然重新 `watch()` 重建监视。

## 7. 下一步学习建议

- **横向连读**：把本讲和上一讲 u7-l1「HTTP 热重载服务器」连起来看——`watcher` 负责「发现文件变了、唤醒重编译」，`HttpServer` 负责「编译完把新产物推给浏览器刷新」。两者共同构成 `typst watch --features http-server` 的完整实时体验。建议阅读 [crates/typst-cli/src/watch.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/watch.rs) 全文，看它们如何被同一个 `watch()` 主循环驱动。
- **向上追溯依赖来源**：回顾 u3-l1 / u3-l3，理解 `FileStore::dependencies()`（[src/files.rs:91-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L91-L99)）如何记录「本次编译访问过哪些 FileId」，以及 `SystemFiles::resolve()`（[src/files.rs:304-306](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L304-L306)）如何把它们还原成 watcher 需要的磁盘路径。
- **深入 notify-rs**：若想理解后端差异，可阅读 notify-rs 文档中 `RecommendedWatcher`、`PollWatcher` 与各平台后端（inotify / FSEvents / ReadDirectoryChangesW）的行为差异，重点是「删除/重命名时各后端的隐式 unwatch 语义」——这正是本讲 4.5 workaround 的根源。
- **下一单元**：本单元（u7）到此结束。接下来 u8 将进入「性能追踪与时间处理」，讲解 `Timer`（编译耗时追踪、导出 Chrome tracing JSON）与 `Time`（`World::today` 的可复现构建时间处理）。
