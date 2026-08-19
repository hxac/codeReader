# u2-l4 设备丢失检测：回调、原子标志与上下文校验

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「GPU 设备丢失（device lost）」是什么、什么时候会发生，以及为什么渲染器必须显式检测它而不是等崩溃。
2. 精读 `set_device_lost_callback` 的注册代码：回调在什么约束下被调用、为什么要把 `Destroyed` 原因过滤掉。
3. 解释丢失状态为什么用一个 `Arc<AtomicBool>` 承载，而不是 `WgpuContext` 结构体里的普通 `bool` 字段——这是本讲最核心的设计题。
4. 画出「一个 `WgpuContext` + 多个 `WgpuRenderer` 共享同一个丢失标志」的示意图，并说明 `check_compatible_with_surface` 在第二个窗口复用上下文时防住了什么错误。

本讲全部代码集中在 [src/wgpu_context.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs) 与 [src/wgpu_renderer.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs) 两个文件中。

## 2. 前置知识

### 2.1 什么是「设备丢失」

GPU 是一块独立硬件，操作系统驱动替我们管理它。有些故障会让驱动判定「这个逻辑设备已经不可用」，之后所有对它的操作都会失败，典型诱因：

- 驱动崩溃或驱动内部错误（驱动通常会自动重置显卡来恢复）；
- 系统休眠 / 唤醒、显卡掉电；
- 笔记本外接显卡被拔出、驱动升级；
- 显存耗尽到驱动无法恢复的程度。

这就像 U 盘被拔出后，文件句柄还握在手里，但任何读写都已无意义。wgpu 把这类事件抽象为「device lost」，并给应用一个**回调**在事件发生时收到通知。收到通知不等于能就地修复——`wgpu::Device` 一旦丢失就永久失效，唯一出路是**重建整个上下文**（新 instance → 新 adapter → 新 device）。检测的意义在于：把「静默的、每帧都失败的渲染」变成「一次显式的恢复流程」。完整恢复流程属于 u6-l1 的内容，本讲只负责「检测与共享」这一半。

### 2.2 回调（callback）与 `'static` 约束

回调是一种控制反转：我们不主动去查询，而是把一个函数交给 wgpu，事件发生时 wgpu 来调用它。Rust 里这类闭包参数几乎总要求 `Fn(...) + Send + 'static`：

- `'static`：闭包不能借用外层函数栈上的变量，必须**拥有**自己用到的全部数据（因此代码里出现了 `Arc::clone` 后再 `move`）；
- `Send`：回调可能在另一个线程被触发，必须能跨线程转移。

这两条约束直接决定了丢失标志的类型选择（见 4.2）。

### 2.3 Arc 与 AtomicBool

- `std::sync::Arc<T>`：原子引用计数的智能指针，让**多个所有者**共享同一份堆上数据；最后一个 `Arc` 被丢弃时数据才释放。
- `std::sync::atomic::AtomicBool`：原子布尔。多个线程可以同时读写而不会数据竞争，单次 `load`/`store` 不可分割。
- `Ordering`：内存序，约束这次原子操作与**其他内存操作**的相对顺序。对单个布尔「闩锁」来说 `Relaxed` 已够用（原子性由类型本身保证），本讲末尾的练习会讨论代码中 `Relaxed` 与 `SeqCst` 混用的细节。

### 2.4 承接前几讲

u2-l1 说过：`WgpuContext` 封装 wgpu 的 Instance/Adapter/Device/Queue 四层对象，并持有若干派生能力字段；u2-l2 讲过适配器选择里「轻查 / 重测」的分工，其中轻查函数 `check_compatible_with_surface` 被留到本讲展开；u1-l2 提过类型别名 `GpuContext = Rc<RefCell<Option<WgpuContext>>>` 是多窗口共享上下文的槽位（细节在 u3-l1 展开，本讲只需把它理解为「一个进程内所有窗口可共同读写的、可空的上下文格子」）。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [src/wgpu_context.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs) | **生产者侧**：定义 `device_lost` 字段、注册回调、暴露 `device_lost()` / `device_lost_flag()` 两个读取口，以及 `check_compatible_with_surface` 校验 |
| [src/wgpu_renderer.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs) | **消费者侧**：构造时克隆标志、复用上下文前调用兼容性检查、`draw()` 早退、`recover()` 协调 |
| [crates/gpui_linux/src/linux/wayland/window.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/wayland/window.rs) | 平台层消费示例：Wayland 窗口每帧先轮询 `device_lost()` 再决定恢复或绘制 |
| [crates/gpui_linux/src/linux/x11/window.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/x11/window.rs) | 同上，X11 版本 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**回调注册与原因过滤**、**共享标志 `Arc<AtomicBool>`**、**表面兼容性校验**、**消费链全景**。前两个是「检测」，第三个是「复用防线」，第四个把检测接到恢复的门口。

### 4.1 `set_device_lost_callback`：把驱动事件翻译成一个布尔

#### 4.1.1 概念说明

wgpu 在创建 `Device` 后允许注册一个丢失回调。本 crate 的做法非常克制：回调里**不做任何恢复动作**，只做两件事——打一条错误日志、把一个布尔写成 `true`。恢复被完全推迟到帧循环的显式轮询里（见 4.4）。

这种「事件 → 单个布尔闩锁」的翻译是刻意的简化：

- 回调发生的时机与线程都不可控，在回调里重建 device 是危险的（可能重入、可能在驱动正在拆栈的线程上）；
- 渲染侧只需要一个二值答案「还能不能用」，闩锁足够；
- 丢失是不可逆事件，标志**只置位、永不清零**，语义上是单调的。

#### 4.1.2 核心流程

原生路径（`new_with_options`）在拿到 device/queue 之后立即注册：

```text
create_device 成功，得到 device
  ↓
分配堆上的原子布尔 device_lost = false
  ↓
device.set_device_lost_callback( 闭包 )
    闭包捕获 = Arc::clone(&device_lost)   ← move 进 'static 闭包
  ↓
把 device_lost 存入 WgpuContext.device_lost 字段
```

回调被触发时（任意时机、可能任意线程）：

```text
输入: reason, message
  ↓
log::error!("wgpu device lost: ...")        ← 无条件记录
  ↓
reason == Destroyed ?
    是 → 什么都不做（这是我们自己主动 drop 了 device，正常生命周期）
    否 → device_lost.store(true, Relaxed)   ← 异常丢失，置位闩锁
```

关于 `reason`：wgpu 用 `wgpu::DeviceLostReason` 描述丢失原因，在 wgpu 29 中取值为 `Destroyed`（应用主动销毁了 device）与 `Unknown`（驱动崩溃、设备移除等一切异常原因的统称）。你可以在本地依赖源码里搜索 `enum DeviceLostReason` 验证（`~/.cargo/registry/src/.../wgpu-types-29.0.4/`）。

为什么要过滤 `Destroyed`？因为「丢失」这个词把两类完全不同的事件混在了一起：

| 原因 | 谁发起 | 是否异常 | 应该置位吗 |
| --- | --- | --- | --- |
| `Destroyed` | 我们自己的代码 drop 了 `Device`（如窗口关闭、恢复流程中释放旧资源） | 否，正常生命周期 | 否 |
| 其他（`Unknown` 等） | 驱动 / 系统 | 是，设备真的坏了 | 是 |

不过滤的后果：每次应用正常关闭或恢复流程主动释放旧 device 时，回调都会把标志打成 `true`，日志里出现误导性的 "device lost"，更糟的是恢复流程中「旧的假阳性标志」可能被误读为「新上下文又丢了」，引发无意义的重试循环。

#### 4.1.3 源码精读

先看字段定义。`WgpuContext` 把丢失标志与 instance/adapter/device/queue 并列为成员：

[wgpu_context.rs:L9-L18](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L9-L18) —— `device_lost: Arc<AtomicBool>` 是结构体的最后一个私有字段（L17），与 `backend`、`dual_source_blending` 等派生能力放在一起；`device` 与 `queue` 被包在 `Arc` 里（L12-L13），是为了后续能把它们移动进 `WgpuResources` 而不借用整个上下文。

原生路径的注册代码：

[wgpu_context.rs:L114-L123](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L114-L123) —— 这 10 行是本模块的心脏：堆上分配 `AtomicBool::new(false)`；`Arc::clone(&device_lost)` 后 `move` 进闭包，让闭包与结构体**各持一个指向同一份布尔的句柄**；回调无条件打日志，仅当 `reason != wgpu::DeviceLostReason::Destroyed` 时以 `Ordering::Relaxed` 置位。

注意这段代码出现在 `select_adapter_and_device` 成功之后（L105-L112，u2-l2 已精读）：只有最终选定的 device 才值得注册回调——选卡过程中被淘汰的 device 直接 drop 即可。

Web 路径有一份**几乎逐字相同**的拷贝：

[wgpu_context.rs:L204-L215](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L204-L215) —— wasm 的 `new_web_with_backend` 里同样的 `Arc::new` + `set_device_lost_callback` + `Destroyed` 过滤，只是位置在 `create_device` 之后、构造 `Self` 之前。两处重复没有抽成公共函数，因为闭包要捕获的 `device_lost` 是局部变量，抽出来反而要传参，收益有限。web 侧消费方式不同（无 `recover`，见 4.4），但生产侧完全一致。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认两处注册点的一致性，并理解 `Destroyed` 过滤在真实生命周期中的触发场景。
2. **操作步骤**：
   - 在 `crates/gpui_wgpu` 下执行 `grep -n "set_device_lost_callback" src/*.rs`，应得到且仅得到两处命中（wgpu_context.rs 的 L115 与 L207）。
   - 对两处命中各读上下文 15 行，列出三处不同：注册位置（选卡后 / create_device 后）、闭包外层的日志语句、`Self` 构造时的字段排列。
   - 再执行 `grep -rn "resources = None\|borrow_mut() = None" src/wgpu_renderer.rs`，找到恢复流程中主动丢弃旧 device 的语句（约 L2086-L2087）——这正是 `Destroyed` 回调会被触发的地方。
3. **需要观察的现象**：`set_device_lost_callback` 只在「上下文构造成功」的路径上出现一次；主动丢弃 device 的代码不在本文件里注册任何东西，却能触发本文件的回调。
4. **预期结果**：你能口头回答「`Destroyed` 事件最晚会在什么代码执行时发生」——答案是：恢复流程 `*gpu_context.borrow_mut() = None` 释放旧 `WgpuContext`、其内部 `Arc<wgpu::Device>` 引用计数归零时（若想实验性验证 drop 时回调确实以 `Destroyed` 触发，可在独立小工程里创建 device、注册回调后显式 drop，**待本地验证**——无 GPU 环境下可能拿不到 adapter）。
5. 本实践为源码阅读型，无需运行项目。

#### 4.1.5 小练习与答案

**练习 1**：如果把 L119 的条件删掉（任何原因都置位），说出一个具体受害场景。
**答案**：u6-l1 将精读的 `recover()` 流程会先执行 `self.resources = None; *gpu_context.borrow_mut() = None;` 主动释放旧 device——此时回调以 `Destroyed` 触发并置位旧标志。虽然新渲染器随后会拿到新标志，但若恢复失败、下一帧读到残留的假阳性标志，就会再次进入恢复分支，形成不必要的重试循环；同时日志每次窗口关闭都会刷 "device lost"，掩盖真实故障。

**练习 2**：为什么回调里只 `store(true)`，从不 `store(false)`？
**答案**：wgpu 的设备丢失是不可逆的——一旦丢失，该 device 上所有资源都失效，不存在「恢复了」的回边。标志因此设计为单调闩锁（monotonic latch），只允许 false → true 一次翻转；「复位」的唯一方式是创建全新上下文、得到全新的 `Arc<AtomicBool>`。

### 4.2 `device_lost_flag`：一份 `Arc<AtomicBool>`，多个渲染器共享

#### 4.2.1 概念说明

本模块回答本讲的核心设计题：**为什么丢失状态必须是 `Arc<AtomicBool>`，而不能是 `WgpuContext` 里的普通 `bool`？** 三个理由层层递进，缺一不可：

1. **回调拿不到 `&mut self`**。`set_device_lost_callback` 要求 `+ 'static` 的闭包，它无法借用 `WgpuContext` 的可变引用（上下文此刻还在构造函数里，构造完成后又被多个窗口以共享方式持有）。要修改状态，只能让闭包**拥有**一块独立的可变内存——堆上的 `AtomicBool` 正是为此存在。
2. **回调可能来自其他线程**。`Send` 约束意味着回调可能在 wgpu 内部线程或 device drop 时的任意线程执行。普通 `bool` 的并发读写是数据竞争（未定义行为）；`AtomicBool` 保证单次 `load`/`store` 原子且无锁，`Arc` 的引用计数本身也是原子的，二者组合是跨线程共享可变布尔的**最小**手段（比 `Mutex<bool>` 更轻——没有锁、没有阻塞、不会死锁）。
3. **多个渲染器要观察同一个标志**。一个 `WgpuContext` 可能被多个窗口共享（u3-l1 展开），每个 `WgpuRenderer` 都需要在 `draw()` 里回答「device 还活着吗」。`Arc::clone` 让 N 个渲染器 + 1 个闭包指向**同一份**布尔，而不是 N 份拷贝——否则驱动崩溃只会有一个渲染器知道。

顺带一提可见性语义：对同一个原子变量，Rust 内存模型保证存在全序的「修改顺序」，且一旦某次读取观察到 `true`，之后的读取不会回退到 `false`。对本标志而言，`Relaxed` 已满足「最终能被轮询线程观察到」的需求，因为它不承担为**其他**数据做同步的职责——丢失发生后，恢复本来就要整体重建，不依赖标志附带任何顺序保证。

#### 4.2.2 核心流程

标志从创建到被观察的完整传播路径：

```text
                ┌──────────────────────────── WgpuContext ────────────────────────────┐
                │  instance   adapter   device(Arc)   queue(Arc)   device_lost ──┐    │
                └──────────────────────────────────────────────────────│────────┘    │
                                     ▲                                    │             │
                     注册时 clone 进闭包                              Arc 指向同一份堆内存
                                     │                                    ▼             │
   驱动崩溃 ──► wgpu 触发回调 ──► 闭包 store(true)  ──────────►  [堆上 AtomicBool]     │
                                     .                    store     ▲   ▲   ▲        │
                                     .                              │   │   │        │
        窗口 A 的 WgpuRenderer ── device_lost.clone ────────────────┘   │   │        │
        窗口 B 的 WgpuRenderer ── device_lost.clone ─────────────────────┘   │        │
        窗口 C 的 WgpuRenderer ── device_lost.clone ───────────────────────────┘        │
                .                    │                                                │
                .              new_internal 里                                       │
                .        context.device_lost_flag() ◄────────────────────────────────┘
```

闩锁语义可以写成一条单调递推（\( f_t \) 表示 \( t \) 时刻标志值，\( l_t \) 表示该时刻是否发生了原因非 `Destroyed` 的丢失事件）：

\[ f_{t+1} = f_t \;\lor\; l_t, \qquad f_0 = 0 \]

由归纳立得 \( f_t \) 单调不减，且 \( f_t = 1 \) 是「上下文需要重建」的充要指示。

#### 4.2.3 源码精读

上下文侧的两个读取口：

[wgpu_context.rs:L556-L565](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L556-L565) —— `device_lost()`（L558-L560）以 `Relaxed` 读取，文档注释点明语义：返回 true 时**应当重建整个上下文**；`device_lost_flag()`（L563-L565）标记为 `pub(crate)`，仅 crate 内部（渲染器）可用，返回 `Arc::clone`——注意它克隆的是**句柄**而非值，开销只是一次引用计数自增。

渲染器侧的持有与克隆点：

[wgpu_renderer.rs:L226-L234](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L226-L234) —— `WgpuRenderer` 的字段列表末尾有 `device_lost: std::sync::atomic::AtomicBool 的 Arc`（L231）与 `surface_configured`、`needs_redraw` 相邻，说明「设备级状态」与「表面级状态」被刻意分开存放。

[wgpu_renderer.rs:L604](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L604) —— `new_internal` 里的 `device_lost: context.device_lost_flag()` 是唯一的克隆点。因为原生 `new`、wasm `new_from_surface`、恢复流程 `recover` 最终都汇聚到 `new_internal`，所以**每一个渲染器**——无论首窗、次窗还是恢复后重建的——拿到的都是指向同一份布尔的克隆。

渲染器侧的轮询口（注意内存序与上下文侧不同）：

[wgpu_renderer.rs:L2047-L2050](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2047-L2050) —— `WgpuRenderer::device_lost()` 以 `Ordering::SeqCst` 读取。`Relaxed`（上下文侧）与 `SeqCst`（渲染器侧）对这个单布尔闩锁都是正确的——原子类型本身保证不会读到「半个值」，内存序只影响与其他变量间的顺序约束；两处选择不一致更像历史演化痕迹，练习 3 会让你分析这件事。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证「所有渲染器共享同一份布尔」，并写出设计理由。
2. **操作步骤**：
   - `grep -n "device_lost_flag" src/wgpu_renderer.rs src/wgpu_context.rs`，确认全 crate 只有 `new_internal` 一处调用（渲染器侧 L604）。
   - `grep -n "new_internal(" src/wgpu_renderer.rs`，列出所有入口：原生 `new`（约 L307）、wasm `new_from_surface`（约 L338）、`recover`（约 L2122）——它们都汇入同一克隆点。
   - 用 4.2.2 的示意图为「三窗口场景」画一版自己的时序图：驱动崩溃 → 回调置位 → 下一帧三个窗口的 `device_lost()` 各自返回 true。
   - 写一段 5 句以内的分析，回答规格中的问题：为什么是 `Arc<AtomicBool>` 而不是结构体里的 `bool`（对照 4.2.1 的三条理由，用你自己的话）。
3. **需要观察的现象**：grep 结果显示 `device_lost_flag` 的可见性是 `pub(crate)`——外部消费者（如 gpui_linux）无法拿到 `Arc` 本身，只能通过 `WgpuRenderer::device_lost()` 轮询；`Arc` 的共享只发生在 crate 内部。
4. **预期结果**：你能在不看书的情况下白板画出「1 个闭包 + 1 个 `WgpuContext` + N 个 `WgpuRenderer` 指向同一块堆内存」的图，并解释每条边是谁在哪行代码建立的（注册边：wgpu_context.rs L115-L122；克隆边：wgpu_renderer.rs L604）。
5. 本实践无需运行项目，重点产出是图与分析文字。

#### 4.2.5 小练习与答案

**练习 1**：把 `Arc<AtomicBool>` 换成 `Arc<Mutex<bool>>` 功能上也成立，为什么作者没这么做？
**答案**：功能上成立但代价更高：`Mutex` 有锁开销、存在死锁面（回调若在持锁时再触发同步操作）、且 `lock().unwrap()` 引入恐慌路径。这里只有单个布尔、无多字段一致性需求、读多写一（一次写、每帧多次读），原子变量是无锁且足够的选择。

**练习 2**：回调闭包里为什么必须先 `Arc::clone(&device_lost)` 再 `move`，直接 `move device_lost` 行不行？
**答案**：不行。`move device_lost` 会把所有权整个交给闭包，`WgpuContext` 结构体就没有这个字段可存了（wgpu_context.rs L140 构造 `Self { ..., device_lost }` 需要保留一份）。`Arc` 的意义正在于此：clone 一份句柄给闭包，原句柄留给结构体，两者指向同一份堆上布尔。

**练习 3**：回调里 `store(true, Relaxed)`，渲染器里 `load(SeqCst)`，两种内存序混用有没有正确性问题？
**答案**：没有。对单个原子布尔闩锁：原子性（不会撕裂）由类型保证；同一位置的修改顺序全序保证读到 `true` 后不会回退。`Relaxed` 与 `SeqCst` 的差异只在「与其他内存操作的相对顺序」——本标志不承担为其他数据建立 happens-before 的职责（丢失后走全量重建，不依赖标志附带的顺序），因此两种序都正确，`SeqCst` 只是更保守的选择。

### 4.3 `check_compatible_with_surface`：第二个窗口的入场检查

#### 4.3.1 概念说明

回忆 u2-l2 的结论：适配器选择发生在**第一个窗口**创建时，代价高昂（枚举、排序、逐个实测）。此后进程内所有窗口复用同一个 `WgpuContext`——instance 和 adapter 都已定死。问题来了：**新窗口的表面（surface）未必与旧 adapter 兼容**。典型场景是混合 GPU 笔记本接了多台显示器：不同输出可能由不同显卡驱动，第一扇窗口选中的 adapter 对第二扇窗口所在的显示器可能一个可用的表面格式都给不出。

如果不检查，这个不兼容会在更深处爆发——`new_internal` 里 `surface.configure` 时驱动报错或恐慌，报错信息离病灶很远。`check_compatible_with_surface` 是一道**廉价的入场防线**：只调用一次 `get_capabilities`（纯只读查询），发现格式列表为空就立刻带着精确的 adapter 信息报错退出。

它和 u2-l2 的 `try_adapter_with_surface` 构成「轻查 / 重测」分工：

| | `check_compatible_with_surface` | `try_adapter_with_surface` |
| --- | --- | --- |
| 时机 | 第二个及之后的窗口复用上下文时 | 第一个窗口选择 adapter 时 |
| 手段 | 只读查询 `get_capabilities` | 创建 device + 真实 configure 一次表面 + 验证 error scope |
| 代价 | 极低 | 高（但成功产物直接复用） |
| 能防住 | 完全不兼容（formats 为空） | 混合 GPU 假阳性（谎报兼容） |
| 防不住 | 谎报兼容的边角案例 | —— |

轻查防不住「`get_capabilities` 谎报」的边角案例——但这在复用路径上可接受：上下文创建时已经做过重测，复用时只需排除最粗粒度的不兼容。

#### 4.3.2 核心流程

`WgpuRenderer::new`（原生路径）的决策树：

```text
拿到窗口句柄，构造 surface（必须用共享 context 的同一个 instance，
                                  否则 wgpu 会 panic —— 代码注释原话）
  ↓
borrow_mut 共享槽 GpuContext
  ↓
槽里有 WgpuContext 吗？
  ├─ 有  → check_compatible_with_surface(&surface)
  │          ├─ caps.formats 非空 → 复用该 context，继续 new_internal
  │          └─ caps.formats 为空 → 报错 "Adapter ... is not compatible
  │                                 with the display surface for this window"
  └─ 没有 → WgpuContext::new(...)（走 u2-l2 的完整选卡流程）并插入槽内
  ↓
new_internal(... device_lost_flag() 在这里被克隆 ...)
```

#### 4.3.3 源码精读

校验函数本体：

[wgpu_context.rs:L300-L313](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L300-L313) —— 用 `surface.get_capabilities(&self.adapter)` 查询「**这个** adapter 对**这个**表面」支持的能力；仅当 `caps.formats.is_empty()` 时 bail，错误信息带上 adapter 名字、后端与 device 号（与 u2-l2 选卡日志的字段一致，方便运维对照）；格式列表非空即认为通过，**不**逐项验证 alpha mode 等。

调用方（复用分支）：

[wgpu_renderer.rs:L278-L303](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L278-L303) —— L281-L285：优先从共享槽克隆已有 instance（并附注释说明表面必须与 adapter 选择用同一个 instance 创建，否则 wgpu 会 panic）；L296-L303 是本模块关键分支：`Some(context)` 走轻查（L299 的 `?` 把不兼容直接上抛为构造失败），`None` 才创建新上下文并插入。注意 `borrow_mut` 的借用窗口很短，分支结束即释放，不会跨 `new_internal` 持有（`new_internal` 接收的是 `&WgpuContext`，来自 L302 `insert` 的返回或 L300 的匹配值）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：搞清「没有这道检查会发生什么」，把防线价值说具体。
2. **操作步骤**：
   - 精读 [wgpu_renderer.rs:L278-L303](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L278-L303)，在 L299 处假设删掉 `?` 与整行，追踪后续调用：`new_internal` → 表面 configure → 什么时机、以什么形式失败？
   - 对照 [wgpu_context.rs:L464-L467](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L464-L467)：`try_adapter_with_surface` 开头有同样的 `caps.formats.is_empty()` 判断——两处同构并非巧合，选卡阶段与复用阶段各自守一道。
   - 写下你的结论：第二个窗口在不兼容显示器上打开时，有检查 = 构造 `WgpuRenderer` 立即返回带 adapter 信息的 `Err`；无检查 = 错误推迟到 configure/首帧 draw，表现为驱动层报错或更糟的静默黑窗。
3. **需要观察的现象**：错误信息里 `device={:#06x}` 与 u2-l2 适配器排序日志用的字段一致，可交叉定位是哪块卡拒绝了新窗口。
4. **预期结果**：你能复述「轻查发生在 borrow_mut 的借用窗口内、失败经 `?` 上抛、成功则复用 context 并在 `new_internal` 克隆同一份丢失标志」这条链路。
5. 混合 GPU + 多显示器的真实触发场景**待本地验证**（需要 Intel+NVIDIA 双卡与多输出环境）；无硬件时以源码推演为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么复用路径只查 `formats`，不也做一次 u2-l2 式的「真实 configure 测试」？
**答案**：重测的收益是识破「谎报兼容」，但那类假阳性主要出现在**选卡**阶段（决定用哪块卡）；复用阶段 adapter 已定、无法更换——即便测出问题，出路也只是报错让上层换窗口或重建上下文。轻查能以极低代价排除「完全不兼容」这一最常见情形，性价比最优；真遇到边角案例，后续 configure/draw 的错误处理（u3-l4 的帧错误计数）仍会兜底。

**练习 2**：第二窗口创建 surface 时为什么必须用共享槽里 context 的 instance，不能自己 new 一个？
**答案**：wgpu 要求 surface 与 adapter/device 属于同一个 instance（同一个驱动连接入口），跨 instance 的组合会在内部校验中 panic。代码在 L278-L285 的注释里明说了这一点，并据此先克隆 instance 再建 surface；这也是 `GpuContext` 槽要连 instance 一起共享的原因之一。

### 4.4 消费链全景：从回调触发到恢复的门口

#### 4.4.1 概念说明

生产侧（4.1、4.2）解决了「谁来写标志」，本模块鸟瞰「谁来读、读了做什么」。读取方有三类，各自的响应策略不同：

1. **平台层每帧轮询**（gpui_linux 的 Wayland/X11 窗口）：`draw` 之前先问 `renderer.device_lost()`，true 则调 `recover()` 重建上下文；
2. **渲染器 wasm 早退**（`WgpuRenderer::draw` 开头）：Web 上没有 `recover`，只能置 `surface_configured = false` 停止渲染并提示用户刷新页面；
3. **恢复流程自身**（`WgpuRenderer::recover`）：判断共享槽里的 context 是否已被**别的窗口**先行恢复，避免重复重建。

本模块只讲到「门口」——`recover` 的完整多窗口协调是 u6-l1 的主角。

#### 4.4.2 核心流程

```text
每帧平台层 draw(scene)
  ↓
renderer.device_lost() ?  ── false ──► 正常渲染 renderer.draw(scene)
  │ true
  ↓
原生（Wayland/X11）: renderer.recover(&raw_window)
    ├─ 共享槽为空或槽内 ctx.device_lost() → 我是第一个发现者，重建整个 context
    └─ 槽内 context 已恢复（标志已是新的一份，false）→ 我只需重建自己的表面与资源

wasm: draw() 开头检测 device_lost()
  ├─ surface_configured 仍为 true → 打日志 "Reload the page to recover"，置 false
  └─ 之后每帧直接 return false（渲染停止，等页面刷新）
```

#### 4.4.3 源码精读

渲染器的轮询口与 wasm 早退：

[wgpu_renderer.rs:L1265-L1275](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L1265-L1275) —— `draw()` 的第一段（wasm 专属 `cfg`）：检测到丢失后只做两件事——打一条「浏览器图形上下文已丢失，请刷新页面恢复」的错误日志、把 `surface_configured` 翻成 `false`；随后 `return false`（false 表示本帧未成功提交，供平台层决策）。注意它读的是**自己字段里的克隆**（L231），而非去问 context——这正是 4.2 共享设计的受益点：渲染器不需要持有 `WgpuContext` 的引用也能感知设备级事件。

恢复流程对共享标志的妙用：

[wgpu_renderer.rs:L2058-L2076](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2058-L2076) —— `recover()` 的文档注释写明多窗口协调策略：第一个调用的窗口重建共享 context，后续窗口直接领用。L2073-L2076 用 `is_none_or(|ctx| ctx.device_lost())` 做判定——这里读的是 `WgpuContext::device_lost()`（上下文侧那个 `Relaxed` 版本）：若共享槽里的 context「不存在」或「自己的标志仍是 true」，说明还没人完成恢复，本窗口担任重建者。**重建成功后槽里换上的是全新 context、全新的 `Arc<AtomicBool>`**——复位闩锁的方式不是清零，而是换新，与 4.1.5 练习 2 的结论闭环。

平台层消费示例（Wayland）：

[crates/gpui_linux/src/linux/wayland/window.rs:L1707-L1730](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/wayland/window.rs#L1707-L1730) —— 平台的 `draw` 入口第一件事就是轮询：`state.renderer.device_lost()` 为 true 时构造原始窗口句柄、调用 `recover`；失败只 `log::warn!` 声明「下一帧重试」而不 panic；无论成败都置 `force_render_after_recovery = true` 并**跳过本帧渲染**（`return`）。X11 侧同构，见 [crates/gpui_linux/src/linux/x11/window.rs:L1706-L1726](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/x11/window.rs#L1706-L1726)。

#### 4.4.4 代码实践（调用链跟踪型）

1. **实践目标**：完整走一遍「回调置位 → 平台轮询 → recover 判定」的跨文件调用链。
2. **操作步骤**：
   - 从 [wgpu_context.rs:L117-L122](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L117-L122) 的闭包出发，依次跳转：`device_lost.store` → [wgpu_renderer.rs:L2048-L2050](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2048-L2050)（渲染器轮询）→ [wayland/window.rs:L1710](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/wayland/window.rs#L1710)（平台轮询）→ [wgpu_renderer.rs:L2073-L2076](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L2073-L2076)（recover 判定）。共 5 个文件内位置，画成一张标注了行号的调用链图。
   - 对比 wasm 分支：同一个标志，为什么 [wgpu_renderer.rs:L1267-L1274](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L1267-L1274) 的响应是「停止渲染 + 提示刷新」而不是 recover？
3. **需要观察的现象**：轮询点全部位于帧循环的**开头**（平台 `draw` 第一句、渲染器 `draw` 第一段），没有任何地方阻塞等待回调——回调与轮询在时间上是解耦的。
4. **预期结果**：你能解释「为什么每帧轮询而不是只依赖回调」——回调负责**最早**感知，轮询负责在**安全时机**（帧边界、主线程）响应；两者配合避免了在回调线程里做危险的重活。
5. 真实触发设备丢失需要驱动级故障或休眠唤醒，**待本地验证**；源码推演链路完整闭合。

#### 4.4.5 小练习与答案

**练习 1**：wasm 下为什么不能像原生那样 `recover()`？
**答案**：原生恢复依赖「释放旧 device、重新枚举 adapter、重建 context」这套进程内操作；而 Web 上 device 丢失通常意味着浏览器侧 WebGPU/WebGL 上下文报废，wasm 环境也没有多窗口共享 `GpuContext`（`WgpuRenderer` 的 `context` 字段在 wasm 下是 `None`，见 L207-L209 的 `#[allow(dead_code)]`），恢复的等价物是「用户刷新页面」。代码因此选择打日志、停渲染、每帧直接返回 false。

**练习 2**：`recover` 里判断「别人是否已恢复」为什么用 `ctx.device_lost()`（读旧 context 的标志），而不是比较指针/版本号？
**答案**：因为重建者会把共享槽整体换成**新的** `WgpuContext`——新 context 的标志是全新的一份 `Arc<AtomicBool>`，天然为 false。于是「标志为 false」就成了「这是恢复后的新 context」的判据，无需额外的版本号或指针比较。旧标志永不复用，这正是闩锁「只置位、以换代复位」设计带来的免费收益。

## 5. 综合实践

**任务：写一份《设备丢失标志设计分析》小文档（一页以内）+ 一张共享示意图。**

把本讲四个模块串起来，交付三样东西：

1. **示意图**：仿照 4.2.2，画「1 个丢失回调闭包 + 1 个 `WgpuContext` + 3 个 `WgpuRenderer`（其中 1 个来自恢复重建）」的内存关系图，要求标注：
   - 每条引用边建立的源码位置（注册边：[wgpu_context.rs:L115-L122](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L115-L122)；克隆边：[wgpu_renderer.rs:L604](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L604)）；
   - 驱动崩溃时刻数据的流向（谁 store、谁们 load）；
   - 恢复完成后图的变化（旧 `Arc` 整体被丢弃、新 `Arc` 诞生）。
2. **决策表**：回答「丢失状态为什么这样存」，至少覆盖四行——`bool` 字段 / `Arc<Mutex<bool>>` / `Arc<AtomicBool>` / 回调里直接重建，每行写「被哪条硬约束否决」或「被采纳，理由」（依据：4.2.1 的三条理由 + 4.1 的事件→闩锁哲学）。
3. **防线说明**：用两段话分别说清 `check_compatible_with_surface`（[wgpu_context.rs:L300-L313](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L300-L313)）与 `Destroyed` 过滤各自防住的具体事故（参考 4.3.4 与 4.1.5 的答案）。

**验收标准**：图里的每条边都能给出文件与行号；决策表能说出 `pub(crate)` 可见性为何把 `Arc` 留在 crate 内部；全部结论只基于本讲引用的源码，无需运行项目（涉及真实设备丢失的场景一律标注「待本地验证」）。

## 6. 本讲小结

- 设备丢失是驱动级不可逆事件；本 crate 用 `set_device_lost_callback` 把它翻译成**一个单调的 `Arc<AtomicBool>` 闩锁**，恢复动作完全推迟到帧循环的安全时机。
- 回调过滤 `DeviceLostReason::Destroyed`：自己主动 drop device 是正常生命周期，不过滤会造成假阳性日志与无意义的重试循环。
- 用 `Arc<AtomicBool>` 而非 `bool` 字段的三条硬理由：`'static` 回调拿不到 `&mut self`、回调可能来自其他线程（需要原子而非普通布尔）、多个渲染器要观察**同一份**标志（`Arc::clone` 句柄共享）。
- `device_lost_flag()` 是 `pub(crate)` 的唯一克隆点，位于 `new_internal`——首窗、次窗、恢复重建的渲染器全部经过它拿到同一份布尔的克隆。
- `check_compatible_with_surface` 是多窗口复用上下文时的轻量入场检查（只读查询 `get_capabilities`、formats 为空即报带 adapter 信息的错误），与 u2-l2 选卡阶段的「真实 configure 重测」构成轻查/重测分工。
- 消费链三入口：平台层每帧轮询并触发 `recover`（原生）、`draw()` wasm 早退停渲染提示刷新、`recover()` 用「标志是否为 false」判断别的窗口是否已完成恢复——复位闩锁的方式是**换新 context**，不是清零。

## 7. 下一步学习建议

- **下一讲 u3-l1（WgpuRenderer 总览）**：本讲反复出现的 `GpuContext = Rc<RefCell<Option<WgpuContext>>>` 共享槽、`new` → `new_internal` 三段式构造将在那里完整展开，表面格式选择与尺寸钳制也随之登场。
- **u6-l1（设备恢复与多窗口协调）**：本讲 4.4 停在门口的 `recover()` 全流程——350ms 等待、`new_rejecting_software`、`atlas.handle_device_lost` 的分层重建——是那里的主角；学完后建议回头重做本讲综合实践的第 1 项，把恢复后那张图画得更细。
- **延伸阅读**：对照 [wgpu_renderer.rs:L1265-L1294](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_renderer.rs#L1265-L1294)，预习设备丢失之外的另一类帧失败——GPU 错误计数与降级（超过 10 次 panic），它属于 u3-l4 帧生命周期的内容，与本讲的「设备级失败」是两个不同层级的故障模型。
