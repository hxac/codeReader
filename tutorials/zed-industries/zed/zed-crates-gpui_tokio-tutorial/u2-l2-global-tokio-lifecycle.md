# GlobalTokio 与 GPUI Global 机制、生命周期

## 1. 本讲目标

上一讲我们读完了 `init` / `init_from_handle` 两个初始化函数，知道它们最终都把一个 `GlobalTokio` 写进了 GPUI 的全局状态。本讲把镜头对准这个「全局状态」本身，读完本讲你应该能够：

1. 解释 GPUI 的 `Global` 机制：`Global` trait、`App::set_global` / `App::global` / `ReadGlobal::global` 是如何用一个以类型为键的注册表实现「应用级单例」的。
2. 说明 `GlobalTokio.owned_runtime` 为什么设计成 `Option<Runtime>`，以及这如何编码「运行时归我管 / 归调用方管」两种所有权状态。
3. 分析 `Drop` 实现里为什么选 `shutdown_background()`（非阻塞关停）而不是阻塞式关停，以及 GPUI 在 `App` 结构里对全局销毁顺序的刻意安排。

## 2. 前置知识

本讲需要以下几个基础概念，先用通俗语言过一遍：

- **单例（Singleton）**：整个应用只需要一份的对象（比如全局的 Tokio 运行时句柄）。Rust 没有语言级的全局单例，常见做法是把值存进某个「按需取用」的容器。
- **类型即键（`TypeId`）**：Rust 里每个类型都有一个编译期确定的 `TypeId`。用 `HashMap<TypeId, ...>` 就能实现「每种类型最多存一份值」——取值时用类型参数 `G` 指定要取哪个键，无需字符串名字，也无需手动注册。
- **类型擦除（`Box<dyn Any>`）**：往上面这个 HashMap 里存的值是装箱的任意类型。存进去时类型信息被「擦除」，取出来时再用 `downcast_ref::<G>()` 恢复具体类型。这是 Rust 实现异构容器（存各种不同类型的值）的标准手法。
- **RAII 与 `Drop`**：Rust 用「值离开作用域就自动执行 `Drop::drop`」管理资源（内存、文件、线程池都算资源）。`Drop` 里写什么，资源就以什么方式释放——本讲的核心问题之一就是「Tokio 运行时该在 `Drop` 里怎么关」。
- **`HashMap::insert` 的覆盖语义**：向 HashMap 插入已存在的键时，旧值会被**返回**给调用者；如果调用者不接住，旧值当场被 drop。这一点直接决定了「二次 `set_global` 会立刻销毁旧全局」。
- **引用计数（`Rc`）决定生命周期**：GPUI 的 `App` 被放在 `Rc<AppCell>` 里被各处共享，最后一个引用释放时 `App` 才销毁，其全局才随之销毁。测试代码拿到的 `TestAppContext` 就是这样一个共享者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_tokio/src/gpui_tokio.rs` | 本讲主角：`GlobalTokio` 结构体、`Global`/`Drop` 实现，以及两个 init 函数如何写入它 |
| `crates/gpui/src/global.rs` | GPUI 全局机制的 trait 层：`Global`（标记 trait）、`ReadGlobal`、`UpdateGlobal` |
| `crates/gpui/src/app.rs` | GPUI 全局机制的存储层：`App` 结构里的 `globals_by_type` 字段与 `set_global`/`global`/`remove_global` 等方法 |
| `crates/gpui/src/gpui.rs`（辅助） | `AppContext::read_global` 与 `BorrowAppContext::set_global` 的 trait 声明——解释为什么任何上下文都能读写全局 |
| `crates/gpui/src/app/test_context.rs`（辅助） | `TestAppContext` 持有 `Rc<AppCell>`，用于理解测试环境下全局何时销毁 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. GPUI 的 `Global` 机制：以类型为键的注册表。
2. `GlobalTokio`：借助该机制成为应用级单例，`owned_runtime` 的 `Option` 设计。
3. `Drop` 与 `shutdown_background`：关停时机的取舍。

### 4.1 GPUI 的 Global 机制：一个以类型为键的注册表

#### 4.1.1 概念说明

很多框架都有「全局状态」：应用任何角落都能访问的共享对象。GPUI 的做法不是静态全局变量，而是在 `App` 上下文里维护一个 `TypeIdHashMap<Box<dyn Any>>`——你可以把它想象成一个「以类型为键的保险柜」：

- 存入：`cx.set_global(MyType { ... })`，键是 `TypeId::of::<MyType>()`。
- 取出：`cx.global::<MyType>()`，同一个键，取回 `&MyType`。
- 每种类型只能存一份，后写覆盖先写。

要获得「存取资格」，类型必须实现 `Global` 这个标记 trait。这不是功能要求（trait 本体是空的），而是**类型安全边界**：没实现 `Global` 的类型在编译期就无法进入这套 API，框架（和你自己的 crate）可以借此精确控制「哪些类型允许当全局」。

#### 4.1.2 核心流程

以 `gpui_tokio::init(cx)` 为例，全局的写入与读取流程是：

```text
写入：
init(cx)
  └─ cx.set_global(GlobalTokio { ... })
       └─ App::set_global
            ├─ push_effect(NotifyGlobalObservers)   // 通知观察者「这个全局变了」
            └─ globals_by_type.insert(TypeId::of::<GlobalTokio>(), Box::new(global))
                 └─ 若键已存在：旧值被 insert 返回 → 当场 drop（触发旧值的 Drop）

读取（以 Tokio::spawn 为例）：
Tokio::spawn(cx, fut)
  └─ cx.read_global(|tokio: &GlobalTokio, cx| ...)
       └─ App::read_global
            └─ App::global::<GlobalTokio>()
                 └─ globals_by_type.get(&TypeId) → downcast_ref::<GlobalTokio>()
                      └─ 找不到 → panic!("no state of type ... exists")
```

三条要点：

1. **覆盖即销毁**：对同一类型二次 `set_global`，旧值立刻 `Drop`。
2. **缺失即 panic**：读取未初始化的全局不是返回 `None`，而是直接 panic，并且 `#[track_caller]` 会把 panic 位置指向你的调用处。这是一种 fail-fast 设计。
3. **写会通知**：`set_global` / `global_mut` / `remove_global` 都会 push 一个 `NotifyGlobalObservers` 效应，注册了 `cx.observe_global` 的代码会在更新周期末尾被回调。

#### 4.1.3 源码精读

先看 trait 层。`Global` 是一个刻意留空的标记 trait：

> [crates/gpui/src/global.rs:22-27](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/global.rs#L22-L27) —— `pub trait Global: 'static {}`。注释明说：trait 本体故意为空，功能通过带 blanket impl 的附加 trait（`ReadGlobal`、`UpdateGlobal`）挂上去。

接着是读取通道 `ReadGlobal`：

> [crates/gpui/src/global.rs:30-41](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/global.rs#L30-L41) —— `fn global(cx: &App) -> &Self`，文档注明「未赋值时 panic」；随后 `impl<T: Global> ReadGlobal for T` 为所有 `Global` 类型自动实现，内部转调 `cx.global::<T>()`。正是这个 blanket impl 让 `gpui_tokio.rs` 里的 `GlobalTokio::global(cx)` 写法成立。

再看存储层。`App` 结构体里存放全局的字段带着一段非常关键的注释：

> [crates/gpui/src/app.rs:725-729](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L725-L729) —— `globals_by_type: TypeIdHashMap<Box<dyn Any>>`。注释写明「Drop globals last（全局最后销毁）」：必须等实体和回调持有的任务都被标记取消之后才能销毁全局，因为销毁全局会顺带关停 Tokio 运行时，此时再有人尝试 spawn 阻塞式 Tokio 任务就会 panic。这段注释是本讲 4.3 节的直接证据。

写入的实现只有两行：

> [crates/gpui/src/app.rs:2034-2039](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2034-L2039) —— `App::set_global`：先 `push_effect(Effect::NotifyGlobalObservers)`，再 `globals_by_type.insert(global_type, Box::new(global))`。`insert` 的返回值（旧值）没有被接住，因此**覆盖写入时旧全局立即被 drop**。

读取与「缺失即 panic」：

> [crates/gpui/src/app.rs:1995-2002](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L1995-L2002) —— `App::global`：按 `TypeId` 查表、`downcast_ref` 还原类型，查不到就 `panic!("no state of type {} exists", ...)`。`#[track_caller]` 保证 panic 定位到调用方。`try_global`（[crates/gpui/src/app.rs:2004-2009](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2004-L2009)）是它的 `Option` 版本。

那么 `gpui_tokio` 里的 `cx.read_global(...)` 是从哪来的？它来自 `AppContext` trait：

> [crates/gpui/src/gpui.rs:241-244](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L241-L244) —— `AppContext::read_global` 的声明。任何实现了 `AppContext` 的上下文（`App`、`Context<T>`、`TestAppContext`……）都自带读全局的能力，这就是 `Tokio::spawn<C: AppContext>` 能对任意上下文通用的原因。

> [crates/gpui/src/app.rs:2867-2873](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2867-L2873) —— `App` 对该方法的实现：取出 `&G` 后连同 `&App` 一起交给回调，让调用方在一次借用里既拿全局又拿上下文。

写入侧则由 `BorrowAppContext` 打通：

> [crates/gpui/src/gpui.rs:300-319](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/gpui.rs#L300-L319) —— `BorrowAppContext` 声明了 `set_global` 等方法，并用 blanket impl `impl<C: BorrowMut<App>> BorrowAppContext for C` 让所有可借用 `App` 的类型自动获得写全局能力。

最后两个「销毁通道」，实践环节会用到：

> [crates/gpui/src/app.rs:2047-2057](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2047-L2057) —— `remove_global`：把该类型的全局从表里取出并**按值返回**给调用者（所有权转移），不存在则 panic。

> [crates/gpui/src/app.rs:2041-2045](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2041-L2045) —— `clear_globals`：一次性清空全部全局，注意它带 `#[cfg(any(test, feature = "test-support"))]`，只在测试构建里存在。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「全局在 Zed 代码库里被大量使用」的直观感受，并确认 `Global` 是空 trait。
2. **操作步骤**：
   - 在仓库根运行 `grep -rn "impl Global for" crates --include="*.rs" | wc -l`，得到实现总数；
   - 再运行 `grep -rln "impl Global for" crates --include="*.rs" | head -20` 看看哪些 crate 在用；
   - 随机挑两个实现，看它们的字段：是不是也是「私有 struct + 少数公开访问函数」的形态。
3. **需要观察的现象**：实现数量是几十个量级；多数实现紧跟一个私有 struct，外部只能通过方法访问。
4. **预期结果**：你会看到 GPUI 生态里全局单例的惯用形态与 `GlobalTokio` 完全一致。具体数字随版本变化，以本地输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`Global` trait 的本体是空的，那它存在的意义是什么？

**答案**：它是类型安全边界。空 trait 让「谁能当全局」在编译期被显式声明：没实现 `Global` 的类型调用 `set_global`/`global` 直接编译错误；同时功能（`ReadGlobal`、`UpdateGlobal`）通过 blanket impl 附加，trait 本体保持干净。这也能配合可见性做访问控制（见 4.2 节）。

**练习 2**：如果忘了调用 `gpui_tokio::init(cx)` 就使用 `Tokio::spawn`，会发生什么？错误在编译期还是运行期暴露？

**答案**：运行期 panic。链路是 `Tokio::spawn` → `cx.read_global` → `App::global::<GlobalTokio>()`，在 [crates/gpui/src/app.rs:1995-2002](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L1995-L2002) 查表失败，panic 信息为 `no state of type ... exists`；`#[track_caller]` 会把位置指向 spawn 的调用处。这是 GPUI 刻意的 fail-fast：「忘初始化」属于程序 bug，越早炸越好。

**练习 3**：对同一类型调用两次 `set_global`，第一次存入的值什么时候销毁？

**答案**：第二次 `set_global` 执行时立即销毁。因为 `HashMap::insert` 返回旧值，而 [crates/gpui/src/app.rs:2038](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2038) 没有接住返回值，旧 `Box` 在该语句结束处被 drop，其 `Drop::drop` 同步执行。

### 4.2 GlobalTokio：成为应用级单例与 `owned_runtime` 的 Option 设计

#### 4.2.1 概念说明

有了 4.1 的机制，`GlobalTokio` 的「单例」身份就很好理解了：它只是一个实现了 `Global` 的普通 struct，被存进 `App` 的注册表。真正值得琢磨的有两点。

**第一，访问控制。** `GlobalTokio` 是**私有** struct（没有 `pub`），crate 外部根本叫不出这个类型名，也就无法对它调用 `global()`/`set_global()`。外部唯一入口是空结构体 `Tokio` 上的三个静态方法。这正是 `Global` trait 官方文档推荐的「用 Rust 可见性限制全局访问」模式——全局状态私有，公开一层薄封装。

**第二，所有权编码。** 上一讲已经知道有两条初始化路径：`init` 自建运行时，`init_from_handle` 只收外部 `Handle`。两条路径写进同一个 struct，靠的就是 `owned_runtime: Option<Runtime>`：

- `Some(runtime)`——运行时是 `init` 创建的，归我管，销毁时我负责关停；
- `None`——运行时是别人创建后只借了句柄给我，关停责任在创建方，我什么都不做。

一个 `Option` 字段把「所有权与关停责任」变成了可以在 `Drop` 里用 `if let` 判定的状态，而且 `Tokio::spawn` 等读取侧代码对两条路径**完全统一**——它们只碰 `handle` 字段，从不关心 runtime 在谁手里。

#### 4.2.2 核心流程

`GlobalTokio` 的完整生命周期：

```text
诞生   init(cx)                    → owned_runtime = Some(runtime)，handle = 克隆的句柄
     或 init_from_handle(cx, h)    → owned_runtime = None，       handle = 外部句柄
存活   Tokio::spawn / spawn_result → cx.read_global 取 &GlobalTokio，只用 handle 字段
       Tokio::handle(cx)           → GlobalTokio::global(cx).handle.clone()
死亡   App 销毁（或被覆盖/移除）    → Drop::drop
         ├─ owned_runtime = Some → take() 取出 runtime → shutdown_background()（非阻塞关停）
         └─ owned_runtime = None → 什么都不做（责任在创建方）
```

#### 4.2.3 源码精读

结构体、标记实现、析构实现，总共 14 行：

> [crates/gpui_tokio/src/gpui_tokio.rs:35-48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L35-L48) —— 私有 struct `GlobalTokio` 持有 `owned_runtime: Option<tokio::runtime::Runtime>` 与 `handle: tokio::runtime::Handle`；`impl Global for GlobalTokio {}` 是获得全局资格的标记实现；`impl Drop` 在销毁时若拥有运行时则调用 `shutdown_background()`。`Drop` 细节留到 4.3 精读。

两条写入路径的差异只在 `owned_runtime` 的取值：

> [crates/gpui_tokio/src/gpui_tokio.rs:20-24](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L20-L24) —— `init` 的收尾：`runtime.handle().clone()` 拿到廉价句柄，然后 `cx.set_global(GlobalTokio { owned_runtime: Some(runtime), handle })`，运行时所有权交进全局。

> [crates/gpui_tokio/src/gpui_tokio.rs:28-33](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L28-L33) —— `init_from_handle`：`owned_runtime: None`，只保管外部 `Handle`。谁创建运行时，谁负责它的生命周期。

「访问控制」这个设计在 GPUI 文档里有明文背书：

> [crates/gpui/src/global.rs:12-21](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/global.rs#L12-L21) —— `Global` 的文档注释专门有一节「Restricting Access to Globals」：需要限制全局的读写时，可以建一个实现 `Global` 的私有 struct，再用公开的新类型/方法只暴露想要的操作子集。`GlobalTokio`（私有）+ `Tokio`（公开静态方法）正是这个模式的标准落地。

读取侧对两条初始化路径的统一，可以对照两处代码：

> [crates/gpui_tokio/src/gpui_tokio.rs:61-62](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L61-L62) —— `Tokio::spawn` 里的 `cx.read_global(|tokio: &GlobalTokio, cx| ...)`，随后只使用 `tokio.handle.spawn(f)`。注意这里解构出的是 `&GlobalTokio`，代码从头到尾没有碰 `owned_runtime`。

> [crates/gpui_tokio/src/gpui_tokio.rs:97-99](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L97-L99) —— `Tokio::handle`：`GlobalTokio::global(cx).handle.clone()`，直接走 `ReadGlobal` 的 blanket impl，同样只读 `handle`。

#### 4.2.4 代码实践

1. **实践目标**：验证「先 init 后可用」与「未 init 即 panic」，并体会 `Tokio::handle` 是全局最薄的读取出口。
2. **操作步骤**（在你自己的克隆里做，结束后可用 `git checkout -- crates/gpui_tokio` 还原）：
   - 给 `crates/gpui_tokio/Cargo.toml` 追加测试依赖（workspace 惯用写法，参见 [crates/ui/Cargo.toml:35-36](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/ui/Cargo.toml#L35-L36)）：
     ```toml
     [dev-dependencies]
     gpui = { workspace = true, features = ["test-support"] }
     ```
   - 在 `src/gpui_tokio.rs` 末尾追加（示例代码）：
     ```rust
     #[cfg(test)]
     mod handle_tests {
         use gpui::TestAppContext;

         #[gpui::test]
         fn handle_available_after_init(cx: &mut TestAppContext) {
             cx.update(|cx| super::init(cx));
             let _handle = cx.update(|cx| super::Tokio::handle(cx));
             // 走到这里说明全局已就位；若上一行想验证反例，把 init 那行注释掉再跑
         }
     }
     ```
   - 先按上面跑通（`cargo test -p gpui_tokio`），再注释掉 `init` 那行重跑。
3. **需要观察的现象**：注释掉 `init` 后测试失败，panic 信息形如 `no state of type gpui_tokio::GlobalTokio exists`，且位置指向 `Tokio::handle` 内部调用处。
4. **预期结果**：与 [crates/gpui/src/app.rs:2001](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2001) 的 panic 分支一致（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：把 `owned_runtime` 改成恒为 `Some` 的 `Runtime` 字段行不行？为什么？

**答案**：不行。`init_from_handle` 只拿到 `Handle`，拿不到 `Runtime` 的所有权（调用方可能还要继续用运行时，例如把它存在别处或自行管理）。若字段必须是 `Runtime`，`init_from_handle` 这个 API 就无法实现。`Option` 用 `Some/None` 把「是否拥有运行时」编码进数据结构，`Drop` 据此决定是否关停。

**练习 2**：为什么 `GlobalTokio` 不加 `pub`？加了对使用者有什么坏处？

**答案**：一旦公开，任何下游 crate 都能 `set_global` 覆盖或直接摸到 `Runtime`，绕过 `Tokio` 的封装（例如绕过 spawn 的取消联动直接 spawn）。保持私有后，外部只能经 `Tokio::spawn`/`spawn_result`/`handle` 三个出口，契约不会被绕开——这正是 [crates/gpui/src/global.rs:12-21](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/global.rs#L12-L21) 文档描述的访问控制模式。

**练习 3**：`Tokio::handle` 用的是 `GlobalTokio::global(cx)`，而 `Tokio::spawn` 用的是 `cx.read_global(...)`。两者取全局的底层路径有何关系？

**答案**：同一条。前者走 `ReadGlobal` 的 blanket impl（[crates/gpui/src/global.rs:37-41](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/global.rs#L37-L41)），内部转调 `cx.global::<T>()`；后者走 `AppContext::read_global`（[crates/gpui/src/app.rs:2867-2873](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L2867-L2873)），实现里同样是先 `self.global::<G>()` 再交给回调。区别只在是否把 `&App` 一并借给回调。

### 4.3 Drop 与 shutdown_background：关停时机的取舍

#### 4.3.1 概念说明

资源总要释放。`GlobalTokio` 的 `Drop` 面临的问题是：**Tokio 运行时该怎么关**。Tokio 提供了几种关停方式，代价不同：

| 方式 | 行为 | 对调用线程的影响 |
| --- | --- | --- |
| 直接 drop `Runtime`（或阻塞式 shutdown） | 发出关停信号并**等待**所有 worker 线程收尾退出 | 阻塞，时长不可控 |
| `shutdown_timeout(d)` | 最多等 `d`，超时后转为后台关停 | 阻塞，但有上界 |
| `shutdown_background()` | 发出关停信号后**立即返回**，不等待 | 近似零开销 |

关键背景：`GlobalTokio::drop` 运行在 **GPUI 主线程**上——它发生在 `App` 销毁、全局注册表清空的时刻（应用退出路径，或测试结束）。而 Tokio 的取消是协作式的：任务只在 await 点响应取消，一个正卡在长同步计算里的任务可以无限拖延「所有 worker 线程退出」这一刻。若采用阻塞式关停，一次退出可能把主线程挂起任意久，表现为「点关闭，编辑器卡死不退」。

用公式表达这个取舍：设 \(T_{\text{drain}}\) 为从收到关停信号到全部 worker 线程实际退出的耗时，则

\[
T_{\text{阻塞}} \approx T_{\text{drain}} \;(\text{存在不响应取消的任务时 } T_{\text{drain}} \to \infty), \qquad T_{\text{background}} \approx 0
\]

`gpui_tokio` 的选择是：**退出阶段，快速返回优于优雅收尾**——宁可让尚在运行的 Tokio 任务被直接丢弃，也不让主线程悬着。于是 `shutdown_background()` 胜出。

还有一个 GPUI 侧的配套安排：`App` 结构体里 `globals_by_type` 字段被刻意声明在实体等字段之后（Rust 按声明顺序释放字段），保证「全局（连带 Tokio 运行时）最后销毁」——先让实体和回调持有的任务都被标记取消，再关运行时，避免有任务在运行时已死后还想用 Tokio。

#### 4.3.2 核心流程

```text
App 销毁（最后一个 Rc<AppCell> 释放）
  └─ App 各字段按声明顺序 drop
       └─ globals_by_type（刻意排最后）
            └─ 表内每个 Box<dyn Any> 依次 drop
                 └─ GlobalTokio::drop
                      ├─ owned_runtime.take() 得到 Some(runtime)？
                      │    ├─ 是 → runtime.shutdown_background()：发信号、立即返回
                      │    └─ 否 → 什么都不做（运行时归 init_from_handle 的调用方管）
                      └─ handle 字段随后正常 drop（Handle 可廉价克隆，drop 无副作用）
```

两条补充销毁路径（不经过 `App` 销毁，但同样会触发这段 `Drop`）：

- 同类型二次 `set_global`：旧 `GlobalTokio` 当场 drop（见 4.1.3）；
- `remove_global::<GlobalTokio>()`：把值按值取回，调用方 drop 时触发。由于 `GlobalTokio` 私有，这两条只发生在 crate 内部。

#### 4.3.3 源码精读

> [crates/gpui_tokio/src/gpui_tokio.rs:42-48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L42-L48) —— `Drop` 实现全文：`if let Some(runtime) = self.owned_runtime.take() { runtime.shutdown_background(); }`。三个细节：① `take()` 把 `Runtime` 从 `Option` 里按值移出——`shutdown_background(self)` 消耗 self，用 `&self` 是调不成的；② `take()` 同时把字段置为 `None`，天然防止重复关停；③ `None` 分支什么也不做，把关停责任留给 `init_from_handle` 的调用方。

> [crates/gpui/src/app.rs:725-729](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L725-L729) —— GPUI 侧的配套注释：全局必须最后销毁，因为销毁全局会关停 Tokio 运行时，而此时若仍有任务试图 spawn 阻塞式 Tokio 任务就会 panic。这说明 `shutdown_background` 的时机不是孤立的，而是 GPUI 整个销毁顺序设计中的一环。

> [crates/gpui/src/app/test_context.rs:18-34](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app/test_context.rs#L18-L34) —— `TestAppContext` 通过 `app: Rc<AppCell>` 共享持有 `App`。测试函数返回、`TestAppContext` 的克隆全部释放后，`App` 才销毁——这是「每个测试拥有独立全局状态、测试结束各自关停」的机制根源，也解释了为什么每个用到 Tokio 的测试都要自己调一次 `gpui_tokio::init(cx)`。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：亲手复刻 `GlobalTokio` 的形态——一个自定义 `Global` 类型 + `Drop` 日志——并在 headless 测试环境里观察它的**三个销毁时机**：覆盖写入时、`remove_global` 时、`App` 自然销毁时。
2. **操作步骤**（在自己的克隆里做，结束后 `git checkout -- crates/gpui_tokio` 还原）：
   - 保持 4.2.4 添加的 `[dev-dependencies]` 不变；
   - 在 `src/gpui_tokio.rs` 末尾追加测试模块（示例代码）：
     ```rust
     #[cfg(test)]
     mod lifecycle_tests {
         use gpui::{AppContext, Global, TestAppContext};

         /// 示例代码：模仿 GlobalTokio 的自定义全局类型
         struct GlobalMyPool {
             name: &'static str,
         }

         impl Global for GlobalMyPool {}

         impl Drop for GlobalMyPool {
             fn drop(&mut self) {
                 eprintln!("[Drop] GlobalMyPool({}) 被销毁", self.name);
             }
         }

         #[gpui::test]
         fn test_global_lifecycle(cx: &mut TestAppContext) {
             // 时机 1：覆盖写入 —— 旧的 "first" 在第二条 set_global 语句处立即 Drop
             cx.update(|cx| cx.set_global(GlobalMyPool { name: "first" }));
             cx.update(|cx| cx.set_global(GlobalMyPool { name: "second" }));

             // 时机 2：remove_global 把所有权取回，drop(removed) 时触发
             let removed = cx.update(|cx| cx.remove_global::<GlobalMyPool>());
             eprintln!("[test] remove_global 取回了 {}", removed.name);
             drop(removed);

             // 时机 3：什么都不做 —— 测试函数返回、App 销毁时才触发
             cx.update(|cx| cx.set_global(GlobalMyPool { name: "third" }));
             cx.run_until_parked();
             eprintln!("[test] 测试函数即将返回");
         }
     }
     ```
   - 运行 `cargo test -p gpui_tokio lifecycle -- --nocapture`。
3. **需要观察的现象**：
   - `[Drop] ... (first)` 出现在 `[test] remove_global ...` 之前（即第二次 `set_global` 执行时）；
   - `[Drop] ... (second)` 紧跟 `drop(removed)`；
   - `[Drop] ... (third)` 出现在 `[test] 测试函数即将返回` **之后**——它由 `Rc<AppCell>` 释放、`App` 字段逐个 drop 时触发，可能混在测试框架的收尾输出里。
4. **预期结果**：三条 `[Drop]` 日志的相对顺序如上；若把 `(third)` 那条 `set_global` 换成再次覆盖，还能看到它提前销毁。「`(third)` 的确切打印时刻」（与测试框架输出的先后交错）待本地验证。
   - 进阶观察：把 `Drop` 里的 `eprintln!` 换成 `shutdown_background` 之类的真实关停调用前，先想清楚它此时运行在哪个线程（GPUI 主线程）——这正是本讲取舍分析的对象。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `Drop` 实现删掉、让 `Runtime` 字段自然 drop，行为会变成什么样？风险是什么？

**答案**：`Runtime` 的 drop 是阻塞式关停：发出关停信号后等待所有 worker 线程退出。`GlobalTokio::drop` 运行在 GPUI 主线程的应用退出路径上，一旦还有 Tokio 任务卡在不响应取消的长同步段里，主线程会被挂起任意久，用户看到的就是「退出卡死」。`shutdown_background()` 用「放弃等待」换取立即返回。

**练习 2**：`Drop` 里为什么是 `self.owned_runtime.take()` 而不是直接 `if let Some(runtime) = &self.owned_runtime { runtime.shutdown_background(); }`？

**答案**：两层原因。其一，`shutdown_background` 的签名按值消耗 `Runtime`（`fn shutdown_background(self)`），拿着 `&Runtime` 无法调用，必须先把 `Runtime` 从 `Option` 中移动出来，`take()` 正是干这个的；其二，`take()` 顺带把字段置为 `None`，即使 `Drop` 逻辑将来被扩展、`GlobalTokio` 被以某种方式二次处理，也不会重复关停。

**练习 3**：为什么 GPUI 要把 `globals_by_type` 安排在 `App` 字段里靠后的位置（配合 [crates/gpui/src/app.rs:725-729](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui/src/app.rs#L725-L729) 的注释）？

**答案**：Rust 按字段声明顺序销毁 struct。把全局表放最后，能保证实体（entities）和回调先销毁、它们持有的任务先被标记取消，然后才轮到全局表——而销毁 `GlobalTokio` 会关停 Tokio 运行时。顺序反了的话，某个尚未取消的任务可能在运行时已关停后仍尝试 `spawn_blocking`，从而 panic。

## 5. 综合实践

把 4.3.4 的玩具全局升级成**持有真实资源的全局**，串起本讲三个模块：

**任务**：实现一个 `GlobalWorkerPool`——用 `std::thread` 起两个后台线程跑一个共享计数循环（或直接复用 `tokio::runtime::Runtime`），存入 GPUI 全局，`Drop` 时打印日志并通知线程退出。要求：

1. struct 私有、实现 `Global`，外部只暴露 `init_pool(cx)` 与 `pool_status(cx) -> String` 两个函数——复刻 `GlobalTokio` + `Tokio` 的访问控制模式（模块 4.1/4.2）。
2. 用 `Option` 字段区分「线程是我建的」与「句柄是别人给的」两条初始化路径（对应 `init` / `init_from_handle`），`Drop` 只在拥有所有权时收尾（模块 4.2）。
3. 写三个测试：
   - 覆盖写入后，旧实例的 `Drop` 日志先于测试断言出现；
   - `remove_global` 取回后资源由测试代码显式释放；
   - 测试自然结束时最后一个实例被销毁、后台线程退出（模块 4.3）。
4. 对照真实实现收尾：把你的 `Drop` 与 [crates/gpui_tokio/src/gpui_tokio.rs:42-48](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L42-L48) 对比——你的线程通知机制若用 `channel`/`AtomicBool`，思考它与 `shutdown_background`「发信号不等收尾」的对应关系；若你选择了「join 等待线程退出」，你就重演了阻塞式关停的取舍，试着说明什么场景下你的选择反而更合理（提示：资源必须在退出前落盘时）。

**验收标准**：`cargo test` 全绿；能口头回答「这个全局在哪些三个时刻会被销毁、每次销毁运行在哪个线程」。

## 6. 本讲小结

- GPUI 的全局是一个**以 `TypeId` 为键、`Box<dyn Any>` 为值**的注册表（`App.globals_by_type`），每种类型最多一份；`Global` 是空的标记 trait，作用是编译期的准入控制。
- `set_global` 覆盖写入时旧值**立即 drop**；读取缺失的全局直接 **panic**（`#[track_caller]` 定位到调用处）——写入通知观察者、缺失 fail-fast 是这套机制的两个性格。
- `GlobalTokio` 私有 + `Tokio` 公开三个静态方法，是 `Global` 文档「用可见性限制全局访问」模式的标准落地；`owned_runtime: Option<Runtime>` 把「运行时归我管 / 归调用方管」编码进数据结构，读取侧（`spawn`/`handle`）对两条初始化路径完全统一。
- `Drop` 用 `take()` 取出运行时后调用 `shutdown_background()`：非阻塞、立即返回。取舍是「退出阶段快速返回优于优雅收尾」，因为阻塞式关停可能被不响应取消的任务无限拖延，而它恰恰运行在 GPUI 主线程上。
- GPUI 把 `globals_by_type` 刻意排在 `App` 字段的销毁顺序末尾：先取消实体与回调的任务，再关 Tokio 运行时，避免运行时已死后仍有任务尝试 spawn 阻塞式工作。
- 测试环境下每个 `#[gpui::test]` 拿到独立的 `TestAppContext`（内部 `Rc<AppCell>`），全局随 `App` 在测试结束后销毁——这就是每个用到 Tokio 的测试都要自行 `gpui_tokio::init(cx)` 的根源。

## 7. 下一步学习建议

本讲结束，`GlobalTokio` 的「生」与「死」都已读完，但它的「活着时干了什么」还只看了 `cx.read_global` 的第一行。下一讲 **u2-l3《Tokio::spawn：从 GPUI 到 Tokio 的桥接全流程》** 将逐行精读 `Tokio::spawn` 的四步：`handle.spawn` → `abort_handle` → `defer` 守卫 → `background_spawn` 包装。

建议预习：

- 重读 [crates/gpui_tokio/src/gpui_tokio.rs:55-73](https://github.com/zed-industries/zed/blob/b0e37a6c18a6321061ba842e26ee7156729f0870/crates/gpui_tokio/src/gpui_tokio.rs#L55-L73)，标出每一行用到的「本讲知识」：`read_global` 从哪来、`background_spawn` 属于哪个 trait。
- 想想 `Drop` 顺序与 `defer` 守卫的关系：`gpui_util::defer` 的 `Deferred` 也是靠 `Drop` 执行回调的——RAII 在这个 crate 里出现了两次，一次为了「关停」，一次（下一讲）为了「取消」。
