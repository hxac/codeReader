# ListenerOptions 与服务端创建

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `ListenerOptions` 构建器，以链式调用的方式组装出一个 local socket 服务端所需的全部选项；
- 说出 `name`、`nonblocking`、`reclaim_name`、`try_overwrite`、`max_spin_time` 这几个 setter 的含义、默认值，以及它们背后的位标志（bit flags）压缩存储原理；
- 理解 `ListenerNonblockingMode` 四态枚举的精确语义，以及它如何被压进同一个 `u8`；
- 跟踪 `create_sync()` / `create_sync_as()` 一路派发到具体平台 `Listener` 的完整调用链，理解 `traits::Listener::from_options` 是「壳/芯」分层的衔接点；
- 用 `incoming()` 把 `accept()` 包装成一个无限迭代器，写出服务端主循环。

本讲只讲「如何把一个服务端监听器建起来」。连接、读写、拆分等内容留待 u3-l2、u3-l3。

## 2. 前置知识

本讲承接 u2 系列建立的「壳/芯」抽象认知（u2-l2、u2-l3、u2-l4）。回顾三句话：

- local socket 是 interprocess 的跨平台抽象，**当前每个平台只有一个后端**——Windows 用 named pipe，Unix 用 Unix domain socket（UDS）；
- 公共层是「壳」（`local_socket` 模块下的 `Listener`/`Stream` 枚举与 trait），平台后端是「芯」；
- 壳通过 `impmod!` 注入芯、通过 `dispatch!` 把方法调用转发给芯。

你需要再了解两个来自前序讲义的概念：

- **构建器模式（builder pattern）**：一个结构体先 `new()` 出来，再用一连串返回 `Self` 的 setter 方法链式配置，最后用一个 `create_*` 方法消费它、产出最终对象。`ListenerOptions` 就是这种模式。
- **`Name<'n>`（u2-l4）**：local socket 的「已解释含义的名字」外壳。构建器里最重要的字段就是它。

几个新术语：

- **位标志（bit flags）**：把若干个「是/否」开关压缩进一个整数的不同二进制位里，用一个 `u8` 就能存六个开关。
- **名称回收（name reclamation）**：UDS 的监听器被关闭后，socket 文件不会自动消失，留下一个「僵尸 socket」，导致同名服务无法重启。interprocess 会在 drop 时自动 `unlink` 掉它，这就是名称回收。
- **AddrInUse**：地址已被占用。重启同名服务而旧的 socket 文件还在时就会遇到它。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/local_socket/listener/options.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs) | `ListenerOptions` 构建器的全部定义：字段、位标志常量、setter、getter、`create_*` 构造方法。 |
| [src/local_socket/listener/trait.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs) | `traits::Listener` trait、`ListenerNonblockingMode` 四态枚举、`ListenerExt` 与 `Incoming` 迭代器。 |
| [src/local_socket/listener/enum.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs) | 公共 `Listener` 枚举本体，其 `from_options` 把构建器派发给后端。 |
| [src/macros.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs) | `builder_setters!` 宏，自动生成 setter。 |
| [src/os/unix/local_socket/dispatch_sync.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs) / [src/os/windows/local_socket/dispatch_sync.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs) | 后端派发入口 `listen()`，把构建器导向具体后端。 |
| [src/os/unix/uds_local_socket/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs) / [src/os/windows/named_pipe/local_socket/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs) | 两个后端各自对 `traits::Listener` 的 `from_options` 实现，真正消费那些选项。 |
| [examples/local_socket/sync/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs) | 同步服务端示例，本讲实践的参照。 |

## 4. 核心概念与源码讲解

### 4.1 ListenerOptions 构建器：服务端的统一入口

#### 4.1.1 概念说明

创建一个 local socket 服务端，需要回答一连串问题：监听哪个名字？要不要非阻塞？关闭时要不要回收 socket 文件？遇到 `AddrInUse` 要不要强行覆盖？这些问题里有「是/否」开关，也有「带值」字段。

interprocess 用一个构建器 `ListenerOptions` 把这些问题集中起来。它的设计目标是：**让跨平台的公共 API 只面对这一套统一选项，平台差异在最后一步才被消费**。也就是说，无论你最终落在 Windows 还是 Unix，写出来的构建器代码长得一模一样——平台差异被推迟到了 `from_options` 那一层（见 4.4）。

#### 4.1.2 核心流程

服务端创建的典型流程是：

```text
ListenerOptions::new()         // 全部选项取默认值
    .name(name)                // 绑定的名字（Name）
    .nonblocking(mode)         // 非阻塞模式（可选）
    .reclaim_name(true)        // 是否在 drop 时回收（默认 true）
    .try_overwrite(false)      // 是否强行覆盖占用（默认 false）
    .max_spin_time(dur)        // 自旋重试上限（仅 Unix 真正生效）
    .create_sync()             // 消费构建器，产出 Listener
```

注意三个要点：

1. `name` 之外的 setter 全部带 `#[must_use]`——构建器方法返回 `Self`，忘了接住返回值等于白配置。
2. setter 默认值集中在 `new()` 里：只有 `reclaim_name` 默认开启，其余默认关闭。
3. `create_sync()` 会**消费**（`self`，不是 `&self`）整个构建器——配置是一次性的。

#### 4.1.3 源码精读

结构体定义只声明真正会占内存的字段：[src/local_socket/listener/options.rs:17-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L17-L26)。关键字段是 `flags: u8`——六个开关全压在它身上；`name`、`max_spin_time`、`mode`、`security_descriptor` 则是带值的、按平台条件编译的字段。

`new()` 给出全部默认值：[src/local_socket/listener/options.rs:63-78](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L63-L78)。其中 `flags = 1 << SHFT_RECLAIM_NAME`，即只把「名称回收」位置 1，其余位为 0。`name` 用 `Name::invalid()` 填一个占位——稍后必须用 `.name(...)` 覆盖，否则会在构造时报错。

`name` setter 不是手写的，而是由 `builder_setters!` 宏生成：[src/local_socket/listener/options.rs:82-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L82-L85)。宏的定义在 [src/macros.rs:75-85](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/macros.rs#L75-L85)，它为字段 `$name` 生成 `pub fn $name(mut self, $name: $ty) -> Self { self.$name = $name.into(); self }`——一行就给一个链式 setter，配合 `#[must_use]`。

最终消费发生在两个构造方法上：[src/local_socket/listener/options.rs:203-207](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L203-L207)。

- `create_sync()` → `create_sync_as::<Listener>()`；
- `create_sync_as::<L>()` 只有一行：`L::from_options(self)`。

也就是说，构建器自己并不「知道」具体后端，它把 `self` 整个交给泛型类型 `L: traits::Listener` 的 `from_options` 去解释。这是「壳把工作甩给芯」的接口。`create_tokio` / `create_tokio_as`（[src/local_socket/listener/options.rs:214-224](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L214-L224)）是启用 `tokio` feature 后的异步版本，结构与同步版完全对称。

#### 4.1.4 代码实践

本讲的核心实践（也是规格指定的任务）：**用 `try_overwrite(true)` 观察同名服务重启时 `AddrInUse` 的处理**。

1. **实践目标**：体会 `try_overwrite` 与默认行为在面对「僵尸 socket 文件」时的差异。
2. **操作步骤**：
   - 复制 [examples/local_socket/sync/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs) 到自己的小项目，把第 12 行改成开启 `try_overwrite`：

     ```rust
     let listener = ListenerOptions::new()
         .name(name)
         .try_overwrite(true)          // 新增这一行
         .create_sync();
     ```

   - 终端 A 运行服务端 `cargo run --example local_socket_sync_server`，让它**保持运行**（不要退出）。
   - 终端 B 用**相同名字**再启动一次同一个服务端。
3. **需要观察的现象**：
   - 原版示例（无 `try_overwrite`）：终端 B 直接得到 `AddrInUse` 并打印「socket file is occupied」后退出；
   - 改造后（`try_overwrite(true)`）：终端 B 会尝试删除旧的 socket 文件并接管名字。由于终端 A 仍持有一个真正的监听 fd，行为是平台相关的——在 Unix 上，新监听器会成功 `unlink` 文件并 bind 到新 inode，而旧监听器虽仍活着但已被「从名字上挤走」（详见 4.2.3 的平台说明）。留意服务端是否打印了自旋重试的迹象。
4. **预期结果**：开启 `try_overwrite` 后，`AddrInUse` 不再直接报错退出，而是触发覆盖逻辑。若担心覆盖时无限自旋，再加 `.max_spin_time(Duration::from_millis(500))`。
5. **若无法本地运行**：标注「待本地验证」。该行为涉及真实多进程与文件系统竞争，无法仅凭阅读源码给出确切现象。

#### 4.1.5 小练习与答案

**练习 1**：`new()` 之后如果没有调用 `.name(...)` 就直接 `.create_sync()`，会发生什么？

**参考答案**：`name` 仍是 `Name::invalid()`（`NameInner::default()`），它不代表任何合法地址。随后 `from_options` 把这个名字交给后端解析地址时会失败，返回一个 `io::Error`。因此 `.name(...)` 在实践中是必填的，只是类型系统没有强制（`Name` 可以由非法值构造，见 u2-l4）。

**练习 2**：为什么所有 setter 都带 `#[must_use]`，而 `new()` 没有？

**参考答案**：`new()` 的返回值本身就是要被继续链式调用的对象，漏掉它编译器本来就会因为「未使用变量」提醒；但 setter 返回的是「配置后的新构建器」，漏接住返回值（写成 `opts.try_overwrite(true);` 末尾分号）会静默丢弃配置、`opts` 保持原状，这种 bug 极难发现，所以用 `#[must_use]` 强制提示。

### 4.2 位标志压缩存储：用一个 u8 管六个开关

#### 4.2.1 概念说明

`ListenerOptions` 有六个「是/否」开关：非阻塞 accept、非阻塞 stream、回收名称、try_overwrite、是否设置了 mode、是否设置了 max_spin_time。如果每个开关都用一个 `bool` 字段，结构体会多占好几个字节，且每次新增开关都要改结构体布局。

位标志（bit flags）是一种经典技巧：用一个整数的不同二进制位表示不同的开关——第 0 位管开关 A，第 1 位管开关 B，依此推。读取用「与运算 + 比较」，写入用「清位 + 置位」。`ListenerOptions` 正是用一个 `u8`（8 位，够放 6 个开关）来存它们。

#### 4.2.2 核心流程

每个开关分配一个「位移量」`SHFT_*`（即它住在第几位）。位运算的两条基本操作：

- **置位**（把第 `pos` 位设成 `val`）：

  \[
  \text{flags} \;\&\; (\text{ALL\_BITS} \oplus (1 \ll \text{pos})) \;|\; ((\text{val as }u8) \ll \text{pos})
  \]

  其中 `ALL_BITS ^ (1<<pos)` 造出一个「除第 pos 位为 0、其余为 1」的掩码，先用它与运算**清掉**第 pos 位，再把 `val` 左移到第 pos 位后用或运算**写回**。

- **读位**（读第 `pos` 位）：

  \[
  (\text{flags} \;\&\; (1 \ll \text{pos})) \neq 0
  \]

这两个操作正是源码里的 `set_bit` 与 `has_bit`。

#### 4.2.3 源码精读

位常量与 `ALL_BITS` 的定义：[src/local_socket/listener/options.rs:29-37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L29-L37)。注意 `ALL_BITS = (1 << 6) - 1` = `0b111111`，恰好覆盖 6 个开关；`NONBLOCKING_BITS = (1<<0)|(1<<1)` = `0b11`，专门标记那两位是非阻塞位（因为它们要一起改，见 4.3）。

`set_bit` 与 `has_bit` 是两个 `const fn`：[src/local_socket/listener/options.rs:38-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L38-L41)。注意 `set_bit` 里 `flags & (ALL_BITS ^ (1 << pos))` 中的 `ALL_BITS ^` 是为了把第 pos 位**清零**（异或一个全 1 掩码里对应位为 1 的值，等价于「取反那一位」），同时把其它位保留。

各个 setter 的写法略有不同：

- `reclaim_name`、`try_overwrite` 直接用 `set_bit`：[src/local_socket/listener/options.rs:100-103](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L100-L103) 与 [src/local_socket/listener/options.rs:133-136](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L133-L136)。
- `max_spin_time` 不只是置位，还要存下实际的 `Duration`，并置 `SHFT_HAS_MAX_SPIN_TIME` 表示「这个值被显式设置过」：[src/local_socket/listener/options.rs:155-163](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L155-L163)。`mode` 字段同理（[src/local_socket/listener/options.rs:164-169](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L164-L169)）。这就是「带值开关」的模式：一个 `HAS_*` 位记录「是否设置」，真正的值另存。
- getter 都是对应的 `has_bit`，例如 `get_try_overwrite`：[src/local_socket/listener/options.rs:181](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L181)；带值的 getter 用 `has_bit(...).then_some(value)`，在「未设置」时返回 `None`：[src/local_socket/listener/options.rs:186-193](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L186-L193)。

`Debug` 实现是位标志设计的「反向证明」——它先把两个非阻塞位重新组装回 `ListenerNonblockingMode`，再逐字段打印：[src/local_socket/listener/options.rs:232-258](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L232-L258)。可见位标志对外完全透明，调试输出和普通字段无异。

#### 4.2.4 代码实践

**实践目标**：亲手验证位运算的逻辑。

1. 读 [src/local_socket/listener/options.rs:38-41](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L38-L41) 的 `set_bit`/`has_bit`。
2. 在自己的草稿里（或一张纸上）模拟：`flags = 0`，依次执行 `set_bit(flags, SHFT_TRY_OVERWRITE=3, true)`、`set_bit(flags, SHFT_RECLAIM_NAME=2, true)`，写出每一步 `flags` 的二进制值。
3. **需要观察的现象**：最终 `flags` 应为 `0b1100`（十进制 12），即第 2、3 位为 1。
4. **预期结果**：与 `new()` 后再链式调用 `.reclaim_name(true).try_overwrite(true)` 得到的 `flags` 一致（注意 `new()` 已经把第 2 位置 1）。
5. 这是纯算术推导，可直接确认，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ALL_BITS` 是 `(1<<6)-1` 而不是 `u8::MAX`（`0xFF`）？

**参考答案**：目前只用到 6 个开关（位移量 0..=5）。把 `ALL_BITS` 限定在低 6 位，能让 `set_bit` 里的清位运算 `ALL_BITS ^ (1<<pos)` 只在有效位范围内翻转，避免高位被意外置 1。一旦未来新增开关，只需把 6 改大、补一个 `SHFT_*` 常量即可，扩展很规整。

**练习 2**：`max_spin_time` 用了「`HAS_*` 位 + 独立字段」，而 `reclaim_name` 只用一个位。为什么 `max_spin_time` 不能也只用一个位？

**参考答案**：`reclaim_name` 的值就是「是/否」，一个位足以表达。但 `max_spin_time` 的值是一个 `Duration`，一个位存不下；所以必须用一个位记录「用户是否设置过」，再用一个独立字段记录「设成了什么」。getter 据此在「未设置」时返回 `None`，让后端能区分「用户显式给 0」与「根本没设置」。

### 4.3 ListenerNonblockingMode：非阻塞的四态枚举

#### 4.3.1 概念说明

非阻塞是 IPC 里容易混淆的概念。对一个监听器来说，「非阻塞」其实涉及**两个独立的对象**：

1. **`accept()` 本身**是否阻塞——没有客户端连进来时，`accept` 是「等」还是「立刻返回 `WouldBlock`」？
2. **`accept` 出来的 stream** 是否非阻塞——后续读写会不会阻塞？

这两个是正交的，组合出 4 种模式：两个都阻塞、只 accept 非阻塞、只 stream 非阻塞、两个都非阻塞。`ListenerNonblockingMode` 就是这 4 种模式的枚举。它被 `nonblocking()` setter 接收。

#### 4.3.2 核心流程

四个变体与两个布尔的关系：

| 变体 | accept 非阻塞 | stream 非阻塞 |
| --- | :---: | :---: |
| `Neither`（默认） | 否 | 否 |
| `Accept` | 是 | 否 |
| `Stream` | 否 | 是 |
| `Both` | 是 | 是 |

枚举用 `#[repr(u8)]`，四个变体的判别值恰好是 `0,1,2,3`，其二进制为 `00,01,10,11`——**bit0 正好等于「accept 非阻塞」，bit1 正好等于「stream 非阻塞」**。这不是巧合，而是被刻意设计成与 4.2 的两个非阻塞位（`SHFT_NONBLOCKING_ACCEPT=0`、`SHFT_NONBLOCKING_STREAM=1`）完全对齐。于是「枚举 ↔ 两个位」之间可以零转换地来回翻译。

#### 4.3.3 源码精读

枚举本体与四态定义：[src/local_socket/listener/trait.rs:64-76](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L64-L76)。注意 `#[repr(u8)]` 和末尾的 `unsafe impl crate::ReprU8`（[src/local_socket/listener/trait.rs:97](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L97)）——后者声明「这个枚举可以安全地当作 `u8` 来存」（Windows 后端会用 `AtomicEnum` 把它原子地存起来）。

三个辅助方法体现了「枚举 ↔ 两布尔」的对偶：[src/local_socket/listener/trait.rs:77-96](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L77-L96)。

- `from_bool(accept, stream)` 用一个 `match (accept, stream)` 把两个布尔组装回枚举；
- `accept_nonblocking()` / `stream_nonblocking()` 分别用 `matches!` 把枚举拆回单个布尔。

setter `nonblocking()` 利用了判别值与位布局的对齐，**一次性**改两位：[src/local_socket/listener/options.rs:91-94](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L91-L94)。`self.flags & (ALL_BITS ^ NONBLOCKING_BITS)` 先把第 0、1 位清零，再 `| nonblocking as u8` 直接把枚举判别值（0..=3）或进去。这就是为什么非阻塞位要单独定义 `NONBLOCKING_BITS` 而不是逐位 `set_bit`——它们要一起替换。

#### 4.3.4 代码实践

**实践目标**：用 `Debug` 输出确认 `nonblocking` 选项被正确压进 `flags`。

1. 写一段小程序（标注为「示例代码」，非项目原有）：

   ```rust
   use interprocess::local_socket::{ListenerOptions, ListenerNonblockingMode};

   fn main() {
       let opts = ListenerOptions::new()
           .nonblocking(ListenerNonblockingMode::Accept);
       println!("{opts:?}");
   }
   ```

2. **操作步骤**：在一个依赖 `interprocess` 的小 crate 里运行。
3. **需要观察的现象**：`Debug` 输出里 `nonblocking` 字段应显示为 `Accept`，而不是 `Neither`。
4. **预期结果**：验证了 `Debug` 通过 `from_bool(get_nonblocking_accept(), get_nonblocking_stream())` 重建出的枚举与设置一致——即位标志的「存」与「读」自洽。
5. 若本地未配置该依赖，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`nonblocking(Stream)` 之后，`get_nonblocking_accept()` 和 `get_nonblocking_stream()` 分别返回什么？

**参考答案**：`Stream` 的判别值是 `2` = `0b10`，bit0=0、bit1=1。`get_nonblocking_accept()` 读 bit0 → `false`；`get_nonblocking_stream()` 读 bit1 → `true`。这与 `ListenerNonblockingMode::Stream` 的语义（只让 stream 非阻塞）一致。

**练习 2**：为什么 setter 用 `nonblocking as u8` 直接或进去，而不是调用两次 `set_bit`？

**参考答案**：因为四个变体的判别值 `0..=3` 的二进制位恰好与「accept 在 bit0、stream 在 bit1」一一对应，所以一次「清两位 + 或入判别值」就能完成，比两次 `set_bit` 更简洁，也不会出现「两位设置到一半」的中间态。

### 4.4 traits::Listener 与 from_options：从构建器到具体监听器

#### 4.4.1 概念说明

`ListenerOptions::create_sync()` 最后调用 `L::from_options(self)`，这里的 `L` 是实现了 `traits::Listener` 的类型。`traits::Listener` 是 local socket 监听器的**接口层**——它声明「一个监听器能做什么」（accept、设置非阻塞、放弃名称回收、从构建器构造），但不说「怎么做」。具体「怎么做」由各平台后端实现。

这是 u2 系列反复出现的「trait 定义接口 + enum 做派发」结构。构建器把 `self` 交给 `from_options`，后者读出那些位标志和带值字段，调用真正的系统调用，产出具体后端的 `Listener`，再包回公共枚举。

#### 4.4.2 核心流程

完整的派发链（以同步、泛型默认 `Listener` 枚举为例）：

```text
ListenerOptions::create_sync()
  → create_sync_as::<Listener>()
    → Listener::from_options(opts)            # 公共枚举的 impl（enum.rs）
      → dispatch::listen(opts)                 # 后端派发入口（dispatch_sync.rs）
        → opts.create_sync_as::<ConcreteBackend>()   # 换成具体后端类型
          → ConcreteBackend::from_options(opts)      # 真正消费选项、调系统调用
        → .map(Listener::from)                 # 把后端包回公共枚举
```

注意第二步到第三步的「再入」：公共 `Listener` 枚举的 `from_options` 不直接造后端，而是交给 `dispatch::listen`，后者再次调用 `create_sync_as`，**但这次泛型参数换成具体后端类型**，于是这次 `from_options` 命中的是后端的实现。整个过程中 `opts` 这个构建器被一路传递，直到后端才被消费。

#### 4.4.3 源码精读

`traits::Listener` trait 的定义：[src/local_socket/listener/trait.rs:17-62](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L17-L62)。它的超 trait 约束很能说明问题——`Iterator<Item = io::Result<Self::Stream>> + FusedIterator + Debug + Send + Sync + Sized + Sealed`：一个监听器本身就是一个「产出连接的迭代器」（这点在 4.5 展开），且是 `Send + Sync`（可跨线程共享）。四个方法：

- `accept(&self)`——核心方法。其文档注释里有一段**Windows 专属的重要警告**：[src/local_socket/listener/trait.rs:28-37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L28-L37)。named pipe 客户端一旦连接就立刻进入「已连接」状态，如果客户端连上又断开、而服务端没在这中间 `accept`，这个管道实例会留下一个「出生即死」的连接，阻塞后续新连接。所以 **Windows 服务端必须周期性地 `accept`**。
- `set_nonblocking(&self, ...)`——运行时改非阻塞模式（注意是 `&self`，靠内部原子实现）。
- `do_not_reclaim_name_on_drop(&mut self)`——关闭时不要回收名字。
- `from_options(options)`——本讲的衔接点：[src/local_socket/listener/trait.rs:56-61](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L56-L61)。文档明确建议不要直接调它，而用 `ListenerOptions` 的 `create_*` 方法。

公共 `Listener` 枚举的 `from_options` 只有一行，转发给后端：[src/local_socket/listener/enum.rs:57-63](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L57-L63)（`dispatch::listen(options)`）。`dispatch::listen` 由 `impmod!` 注入（[src/local_socket/listener/enum.rs:11](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L11)），在 Unix 指向 [src/os/unix/local_socket/dispatch_sync.rs:8-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs#L8-L10)，在 Windows 指向 [src/os/windows/local_socket/dispatch_sync.rs:8-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket/dispatch_sync.rs#L8-L10)。两边的 `listen` 形态完全一致：`options.create_sync_as::<具体后端Listener>().map(Listener::from)`。

两个后端如何**消费**这些选项，差异最明显：

- **Unix 后端**：[src/os/unix/uds_local_socket/listener.rs:32-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L32-L50)。它读 `get_nonblocking_stream()`、`get_nonblocking_accept()`、`get_mode()`，并用 `ReclaimGuard::new(opts.get_reclaim_name(), addr)` 把名称回收语义落进一个 RAII 守卫。真正「遇 AddrInUse 就覆盖」的逻辑在 `listen_and_maybe_overwrite`：[src/os/unix/uds_local_socket.rs:91-116](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket.rs#L91-L116)，其中 `keep_trying_to_overwrite` 判断「`try_overwrite` 开启 **且** 错误是 `AddrInUse`」才重试（[src/os/unix/uds_local_socket.rs:126-128](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket.rs#L126-L128)）。
- **Windows 后端**：[src/os/windows/named_pipe/local_socket/listener.rs:30-42](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L30-L42)。它把 `get_nonblocking_accept/stream` 重组为 `ListenerNonblockingMode` 存进一个 `AtomicEnum`（运行时改非阻塞靠它），把 `name`、`security_descriptor` 映射到 `PipeListenerOptions`。注意：named pipe 无法被覆盖，所以 `try_overwrite` 在 Windows 上**不起作用**（见 setter 文档 [src/local_socket/listener/options.rs:129-130](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L129-L130)）；`do_not_reclaim_name_on_drop` 也是空实现（[src/os/windows/named_pipe/local_socket/listener.rs:59](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L59)），因为 named pipe 没有需要回收的文件。

这就解释了「为什么公共构建器看起来跨平台一致，但某些选项是平台相关的」——平台差异被关在后端的 `from_options` 门后。

#### 4.4.4 代码实践

**实践目标**：跟踪一条完整调用链，理解「壳/芯」如何衔接。

1. 从 [src/local_socket/listener/options.rs:203-207](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L203-L207) 出发。
2. 依次打开并阅读：[enum.rs:57-63](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L57-L63) → 你所在平台的 [dispatch_sync.rs:8-10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/local_socket/dispatch_sync.rs#L8-L10) → 对应后端的 `from_options`（Unix [listener.rs:32-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/uds_local_socket/listener.rs#L32-L50) / Windows [listener.rs:30-42](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L30-L42)）。
3. **需要观察的现象**：在纸上画出这条链，标出每一步 `opts` 是按值还是按引用传递、在哪一步被真正消费。
4. **预期结果**：你会看到 `opts` 以按值方式一路传到后端 `from_options`，在那里才被读取并丢弃；公共层全程没有触碰系统调用。
5. 这是源码阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：公共 `Listener` 枚举的 `from_options` 已经拿到了 `opts`，为什么不直接在后端造监听器，而要先转给 `dispatch::listen`、再「再入」一次 `create_sync_as`？

**参考答案**：这是为了让 `dispatch::listen` 成为唯一的「平台后端选择点」——它由 `impmod!` 按 `cfg(unix)`/`cfg(windows)` 注入，封装了「当前平台用哪个后端」这一信息。公共枚举的代码因此保持平台无关；如果将来一个平台有多个后端，只需改 `dispatch` 层的选路逻辑即可。

**练习 2**：`try_overwrite(true)` 在 Windows 上调用了会有什么效果？

**参考答案**：没有任何效果——`get_try_overwrite()` 在 Windows 后端的 `from_options` 里根本没被读取，named pipe 也不能被覆盖。这个选项是 Unix-only 语义，公共 API 把它统一暴露只是为了跨平台代码不必写 `#[cfg]`。

### 4.5 Incoming：把 accept 变成主循环迭代器

#### 4.5.1 概念说明

服务端的主循环通常是「不停地 accept 新连接、逐个处理」。Rust 里最自然的表达是 `for conn in listener { ... }`。为此，`traits::Listener` 的超 trait 里就包含了 `Iterator<Item = io::Result<Self::Stream>>`——监听器本身就能当迭代器用。

但直接用 `for conn in listener` 会把 `accept` 的 `io::Result` 包在 `Option` 里（迭代器的 `next` 返回 `Option<Item>`），处理起来啰嗦。`Incoming` 是一个更顺手的封装：它持有一个 `&L`，每次 `next` 直接返回 `Some(accept())`，把「永远有下一个连接」这个事实表达成一个无限迭代器，方便用 `.filter_map()`、`.map()` 等组合子过滤掉失败的连接（如示例所做）。

#### 4.5.2 核心流程

```text
listener.incoming()        // 借用 listener，得到 Incoming<'_, L>
  → 迭代器，每次 next() 都 Some(listener.accept())
  → 配合 .filter_map(...).map(BufReader::new) 即成主循环
```

`Incoming` 是无限的：`next` 永不返回 `None`，`size_hint` 报告 `(usize::MAX, None)`。

#### 4.5.3 源码精读

`ListenerExt` trait 提供便利方法 `incoming()`，并对所有 `Listener` 自动实现（blanket impl）：[src/local_socket/listener/trait.rs:99-107](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L99-L107)。`incoming()` 其实就是 `self.into()`。

`Incoming` 结构体与它的 `Iterator` 实现：[src/local_socket/listener/trait.rs:113-126](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/trait.rs#L113-L126)。关键三点：

- `next()` 永远返回 `Some(self.listener.accept())`——把「accept 可能失败」用 `io::Result` 表达，而不是用 `None` 终止迭代；
- `size_hint()` 返回 `(usize::MAX, None)`，明示这是一个无限迭代器；
- 它额外实现了 `FusedIterator`（与 `Listener` 的超 trait 一致）。

公共 `Listener` 枚举也实现了 `Iterator`/`FusedIterator`，其 `next` 就是调用自己的 `accept`：[src/local_socket/listener/enum.rs:77-82](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/enum.rs#L77-L82)。所以你既可以 `for conn in &listener`（借用，走 `Incoming`），也可以直接 `for conn in listener`（消费，走枚举的 `Iterator`）。

示例里的主循环正是用 `incoming()` 配合 `filter_map` 优雅地跳过失败的连接：[examples/local_socket/sync/listener.rs:41-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L41-L45)。

#### 4.5.4 代码实践

**实践目标**：对比「直接迭代」与「incoming + filter_map」两种写法。

1. 读示例 [examples/local_socket/sync/listener.rs:41-45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs#L41-L45)。
2. 想象把它改写成「直接 `for conn in listener`」：你需要在循环体里先 `match conn { Ok(c) => c, Err(e) => { eprintln!(...); continue; } }`，代码更长。
3. **需要观察的现象**：`incoming()` + `filter_map` 把「失败就跳过」的样板压成了一行，主循环体只关心成功的连接。
4. **预期结果**：两种写法行为等价，但 `incoming()` 版更简洁。
5. 这是源码阅读型实践，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：`Incoming::next()` 永远返回 `Some`，那它怎么「结束」？

**参考答案**：它**不会自然结束**——这是一个无限迭代器。主循环会一直跑下去，直到进程被外部信号终止，或者你在循环体里主动 `break`。这也呼应了 `Listener` 的 `FusedIterator` 约束：一旦（理论上）耗尽，之后的 `next` 永远是 `None`，但对 `Incoming` 而言这个状态不会到来。

**练习 2**：为什么 `Incoming` 持有的是 `&'a L` 而不是拥有 `L`？

**参考答案**：`incoming()` 借用监听器（`&self`），这样主循环结束后监听器仍然存在、可以继续使用或被 drop 以触发名称回收。如果它拿走所有权，就无法在循环外控制监听器的生命周期了。

## 5. 综合实践

把本讲的知识串起来，做一个「带覆盖重启能力」的最小 echo 服务端骨架。

**任务**：写一个程序，用 `ListenerOptions` 创建监听器，要求：

1. 名字用 `GenericNamespaced`（参考示例的 `to_ns_name`，见 u2-l4）；
2. 开启 `try_overwrite(true)`，并设置 `max_spin_time(Duration::from_millis(800))` 限制自旋；
3. 用 `incoming()` + `filter_map` 构建主循环，对每个连接读一行、原样写回（echo）；
4. 对 `create_sync()` 的 `AddrInUse` 错误做一次明确打印，区分「普通占用」与「覆盖失败」两种情况。

**操作步骤**：

1. 以 [examples/local_socket/sync/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/examples/local_socket/sync/listener.rs) 为蓝本改造。
2. 把第 12 行的构建器替换为带 `try_overwrite` + `max_spin_time` 的版本。
3. 主循环里把「服务端先发」改成「先 `read_line` 再 `get_mut().write_all` 回写同一行」（注意 u1-l4 提到的半双工收发顺序：一端先发、一端先收，避免死锁）。

**需要观察的现象**：

- 第一次启动正常监听；保持运行时第二次启动，因 `try_overwrite` 不会立刻 `AddrInUse` 退出，而是尝试覆盖（Unix）；若超过 `max_spin_time` 仍失败，才打印覆盖失败并退出。
- echo 行为：客户端发一行、服务端回同一行。

**预期结果**：你得到了一个能优雅处理「僵尸 socket」、且收发顺序不会死锁的最小服务端。

**待本地验证**：覆盖行为涉及多进程与文件系统竞争，具体现象需在本机确认；Windows 上 `try_overwrite` 不生效，需改在 Unix 验证。

## 6. 本讲小结

- `ListenerOptions` 是 local socket 服务端的统一构建器：`new()` 取默认值，链式 setter 配置，`create_sync()` 消费并产出 `Listener`。除 `name` 外的 setter 全部 `#[must_use`。
- 六个「是/否」开关被压进一个 `u8` 的位标志；`set_bit`/`has_bit` 是基本位运算，「带值」选项（`max_spin_time`、`mode`）额外用一个 `HAS_*` 位 + 独立字段。
- `ListenerNonblockingMode` 的四态枚举用 `#[repr(u8)]`，判别值 `0..=3` 的二进制恰好与「accept 在 bit0、stream 在 bit1」对齐，故 `nonblocking()` 可一次替换两位。
- `create_sync()` 经 `create_sync_as` → `L::from_options` → `dispatch::listen` → 后端 `from_options` 一路派发；公共层不碰系统调用，平台差异全在后端（如 `try_overwrite` 仅 Unix 生效）。
- `traits::Listener` 同时是迭代器；`Incoming` 把 `accept` 包装成无限迭代器，配合 `filter_map` 可优雅构建主循环；Windows 上必须周期性 `accept`，否则会留下「出生即死」的连接。
- 默认值：`reclaim_name` 开、`nonblocking` = `Neither`、`try_overwrite` 关。

## 7. 下一步学习建议

- **u3-l2 ConnectOptions 与客户端连接**：对称地看客户端构建器，理解 `ConnectWaitMode` 三态与 `Stream::connect`。
- **u3-l3 Stream 的读写、拆分与重聚**：本讲只把连接 `accept` 出来，下一讲深入 stream 的读写、`split`/`reunite` 与超时。
- **u4-l1 / u4-l2 平台后端实现**：本讲只点到后端 `from_options`，想看清 `ReclaimGuard`、`AtomicEnum`、`listen_and_maybe_overwrite` 的全部细节，进 u4。
- **u9-l2 非阻塞模式、超时与等待模式**：本讲的 `ListenerNonblockingMode` 会在那里与 stream 超时、`ConnectWaitMode` 系统地串联。
