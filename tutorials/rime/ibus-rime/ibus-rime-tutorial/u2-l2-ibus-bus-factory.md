# IBus 总线、工厂与引擎注册

## 1. 本讲目标

上一讲（u2-l1）我们跟着 `main()` 走到了 `rime_with_ibus()` 这一行，然后停了下来。本讲就要钻进 `rime_with_ibus()` 这个函数，搞清楚 ibus-rime 进程是**如何把自己接入 IBus 框架**的——也就是它怎样连上 ibus 守护进程、怎样告诉守护进程「我能提供 Rime 这个输入法引擎」、以及怎样在 D-Bus 总线上占一个名字让别人能找到自己。

学完本讲你应当能够：

- 说清楚 `ibus_init` / `ibus_bus_new` / `ibus_bus_is_connected` 三步如何建立与 ibus 守护进程的连接；
- 说清楚 `ibus_factory_new` / `ibus_factory_add_engine` / `ibus_bus_request_name` 三步如何把 Rime 引擎类型注册出去；
- 解释 `g_object_ref_sink` 在这里的作用，以及 `ibus_bus_request_name` 失败时为什么必须 `exit(1)`；
- 对照 `rime.xml.in`，分清「组件名」与「引擎名」这两个不同命名空间，并把它们对应到代码里。

## 2. 前置知识

本讲要用到几个概念，先通俗地过一遍。

- **IBus 是一个输入法框架，不是一个输入法引擎。** 它在系统里以一个守护进程（`ibus-daemon`）的形式运行，负责统一管理键盘焦点、候选窗口、状态栏图标，以及调度各个输入法。ibus-rime 只是它手下的「一个兵」。
- **D-Bus 是 Linux 桌面的进程间通信总线。** ibus 守护进程和 ibus-rime 是两个独立进程，它们通过 D-Bus 互相对话。一个进程要在 D-Bus 上被别人找到，就需要一个「总线名」（well-known name），例如 `im.rime.Rime`。
- **GObject 是 GLib 的对象系统。** ibus-rime 大量使用 GObject。GObject 对象有「引用计数」来管理生命周期：谁用谁 `ref`（计数 +1），谁不用谁 `unref`（计数 -1），计数归零对象就被销毁。新创建的对象还可能带一个「浮动引用」（floating reference），这是一种特殊的、尚未被任何人「认领」的引用，本讲会详细讲它。
- **工厂模式（Factory）。** 当 ibus 守护进程需要一个新的引擎实例（比如用户切换到 Rime 输入法），它不会自己 `new`，而是让一个「工厂」对象来创建。工厂事先注册了「引擎名 → GType」的映射，到时候按名字就能造出对应类型的对象。

如果对 `main()`、信号回调、`rime_api` 全局指针还不熟悉，请先看 u2-l1。

## 3. 本讲源码地图

本讲主要涉及两个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [rime_main.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c) | 进程入口层，包含 `rime_with_ibus()` | 整个 IBus 接入流程都在这个函数里 |
| [rime.xml.in](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in) | IBus 组件描述模板 | 组件名与引擎名都在这里声明 |

辅助理解还用到两个文件（不在 `source_files` 重点内，但有助于把链路看穿）：

| 文件 | 作用 |
| --- | --- |
| [rime_engine.h](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.h) | 声明 `IBUS_TYPE_RIME_ENGINE` 宏 |
| [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) | 用 `G_DEFINE_TYPE` 真正注册该类型 |

## 4. 核心概念与源码讲解

本讲的全部内容都发生在 `rime_with_ibus()` 这一个函数里（[rime_main.c:94-136](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L94-L136)）。我们按「连接 → 建工厂注册引擎 → 认领总线名」三步把它拆成三个最小模块。函数后半段（librime 的 `setup` / `start`、通知处理）属于 u2-l3 的内容，本讲只在流程图里带过，不展开。

### 4.1 IBusBus 总线：连接到 ibus 守护进程

#### 4.1.1 概念说明

`IBusBus` 是 IBus 库里代表「与 ibus 守护进程的一条连接」的对象。ibus-rime 是一个独立进程（`ibus-engine-rime`），它被 ibus 守护进程通过 `rime.xml.in` 里的 `<exec>` 行启动（见 [rime.xml.in:6](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L6)）。启动之后，它必须**反过来主动连回** ibus 守护进程，才能接收按键事件、回传候选词。

这条连接本质上是一条 D-Bus 连接。`IBusBus` 把这条连接以及围绕它的一组操作（查询是否连上、申请总线名、拿到底层 `GDBusConnection` 等）封装成一个 GObject。

#### 4.1.2 核心流程

建立连接的四步：

1. **`ibus_init()`**：初始化 IBus 库本身（注册 GObject 类型、准备底座）。任何 IBus 调用之前都要先 init。
2. **`ibus_bus_new()`**：创建 `IBusBus` 对象，它内部会打开一条到 ibus 守护进程的 D-Bus 连接。
3. **`g_object_ref_sink(bus)`**：把对象返回的「浮动引用」转成正常的「拥有引用」，明确生命周期归属。
4. **`ibus_bus_is_connected(bus)`**：检查连接是否真的可用。如果没连上（比如当前会话根本不在 IBus 环境下），直接优雅退出。

之后还会用 `g_signal_connect` 给 `bus` 挂一个 `"disconnected"` 信号回调，用于在连接中途断开时通知主循环退出。

整个 `rime_with_ibus()` 的骨架流程如下：

```
rime_with_ibus()
 ├── ibus_init()                                    # ① 初始化 IBus 库
 ├── bus = ibus_bus_new()                           # ② 建立 D-Bus 连接
 ├── g_object_ref_sink(bus)                         # ③ 取得所有权引用
 ├── ibus_bus_is_connected(bus)? 否 → exit(0)        # ④ 连不通就退
 ├── g_signal_connect(bus, "disconnected", ...)     #   挂断连回调
 ├── factory = ibus_factory_new(...)                # ⑤ 建工厂（见 4.2）
 ├── ibus_factory_add_engine(factory, "rime", ...)  # ⑥ 注册引擎（见 4.2）
 ├── ibus_bus_request_name(bus, "im.rime.Rime")     # ⑦ 认领总线名（见 4.3）
 ├── notify_init / set_notification_handler         #   通知（u2-l3）
 ├── rime_api->setup() / ibus_rime_start()          #   librime（u2-l3）
 ├── ibus_main()                                    # ⑧ 阻塞主循环
 ├── ibus_rime_stop() / notify_uninit               #   收尾
 └── g_object_unref(factory); g_object_unref(bus)   # ⑨ 释放
```

#### 4.1.3 源码精读

连接建立的代码非常紧凑（[rime_main.c:95-104](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L95-L104)）：

```c
ibus_init();
IBusBus *bus = ibus_bus_new();
g_object_ref_sink(bus);

if (!ibus_bus_is_connected(bus)) {
  g_warning("not connected to ibus");
  exit(0);
}

g_signal_connect(bus, "disconnected", G_CALLBACK(ibus_disconnect_cb), NULL);
```

逐行说明：

- `ibus_init()` —— 初始化 IBus 库，必须在其它 IBus 调用之前执行。
- `ibus_bus_new()` —— 创建总线对象；内部会尝试建立到 ibus 守护进程的 D-Bus 连接。注意：**创建成功不等于连接成功**，所以下一步要单独检查。
- `g_object_ref_sink(bus)` —— 见下方「为何要 sink」的专段。
- `ibus_bus_is_connected(bus)` —— 判断这条连接是否真的活着。如果返回假，说明当前环境里 ibus 守护进程没在跑、或本进程不在 IBus 会话里，继续下去毫无意义，于是 `exit(0)`。注意这里用的是 **0**（正常退出），因为「没连上」算是一种「无事可做」的优雅情形，而不是错误。
- `g_signal_connect(bus, "disconnected", ...)` —— 给总线挂上 `"disconnected"` 信号。当连接中途断开（比如用户重启了 ibus 守护进程）时，GLib 主循环会触发这个信号，进而调用回调。

断连回调本身只有两行（[rime_main.c:89-92](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L89-L92)）：

```c
static void ibus_disconnect_cb(IBusBus *bus, gpointer user_data) {
  g_debug("bus disconnected");
  ibus_quit();
}
```

`ibus_quit()` 的作用是让稍后的 `ibus_main()` 主循环返回，从而走到函数尾部的 `ibus_rime_stop()` 收尾流程。这和 u2-l1 讲过的 `sigterm_cb` 是同一套「请求主循环退出」的思路——回调里只发退出信号，真正的清理留给主线程的常规退出路径，避免在信号上下文里直接操作 librime。

**为什么需要 `g_object_ref_sink`？** 这是 GObject 的一个关键细节，值得单独讲清：

- `IBusBus`、`IBusFactory` 这些 IBus 对象继承自 `GInitiallyUnowned`，`g_object_new` 创建它们时会带一个**浮动引用（floating reference）**。浮动引用的计数是 1，但它表达的意思是「这个对象还没被任何所有者正式认领」。
- 当一个容器（比如父对象）接收这个对象时，通常调用 `g_object_ref_sink`：若对象仍是浮动的，就把「浮动」标记去掉、变成一个普通引用（**计数不变**），相当于「我认领它了」；若对象已经不浮动，则等价于 `g_object_ref`（**计数 +1**）。
- 在 [rime_main.c:97](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L97) 和 [rime_main.c:107](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L107) 里，代码在 `new` 之后立刻 `g_object_ref_sink`，目的是**把浮动引用转换成一个明确的、由 `rime_with_ibus` 自己拥有的普通引用**，这样后面 `ibus_main()` 阻塞期间对象不会被意外回收，且函数末尾的 `g_object_unref(bus)` / `g_object_unref(factory)`（[rime_main.c:134-135](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L134-L135)）能成对地把计数减回零、干净释放。一句话：**sink 让所有权从「悬而未决」变成「我负责」**。

#### 4.1.4 代码实践

**实践目标：** 理解「连不上就优雅退出」这条分支在什么条件下触发，并验证 `g_object_ref_sink` 的引用计数语义。

**操作步骤：**

1. 打开 [rime_main.c:95-104](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L95-L104)，确认连接检查发生在 `ibus_bus_new` 之后、`ibus_factory_new` 之前。
2. 阅读本段对 `g_object_ref_sink` 的解释，然后在源码里找到与之配对的两个 `g_object_unref`（[rime_main.c:134-135](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L134-L135)），确认「sink 一次、unref 一次」是一一对应的。
3. 想做一个对照实验（可选）：在一个**没有运行 ibus 守护进程**的环境（例如一个干净的容器）里手动执行 `ibus-engine-rime --ibus`（待本地验证），观察它是否会命中 `not connected to ibus` 这条 `g_warning` 并以 `exit(0)` 退出。

**需要观察的现象：**

- 在无 ibus 环境下，进程应当很快退出，而不会卡住或崩溃。
- 退出码为 0（优雅退出），而不是 1（错误）。

**预期结果：** 你能口述出「`ibus_bus_new` 只负责建对象、`ibus_bus_is_connected` 才确认连接真的可用，两者必须分开判断」，并能解释 sink/unref 配对的原因。

> 说明：第 3 步是否可复现取决于你本地的桌面/容器环境，若无法验证请记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `ibus_bus_is_connected` 返回假时用 `exit(0)`，而不是 `exit(1)`？

**参考答案：** 没连上 ibus 守护进程通常意味着当前根本不在 IBus 会话里（比如被误启动、或 ibus 没运行）。这是一种「没有工作可做」的正常情形，不算程序自身出错，所以用 0 表示优雅退出。而后面 `request_name` 失败则是「连上了却拿不到名字」，是真正的异常，用 1。

**练习 2：** 如果删掉 `g_object_ref_sink(bus)` 这一行，程序还能正常跑吗？为什么要保留它？

**参考答案：** 短期内大概率仍能跑，因为浮动引用本身也保持计数为 1，对象不会被立刻回收。但保留 `g_object_ref_sink` 能把「悬而未决」的浮动引用转成明确的所有权引用，避免后续某处代码无意中再次 sink 导致引用计数混乱，也使得函数末尾的 `g_object_unref` 在语义上正确成对。这是一种防御性、显式表达所有权的写法。

---

### 4.2 IBusFactory 工厂与引擎类型注册

#### 4.2.1 概念说明

光有连接还不够。ibus 守护进程还需要知道：当用户切换到 Rime 输入法时，应当由谁来创建引擎实例？答案是 **`IBusFactory`**。

`IBusFactory` 是一个「引擎工厂」对象，它维护一张「引擎名 → GType」的映射表。当守护进程请求某个引擎时，工厂按名字查到对应的 GObject 类型，实例化一个 `IBusRimeEngine` 对象返回。这套机制把「输入法的逻辑实现」（rime_engine.c 里的 GObject）和「IBus 框架的调度」解耦开来。

这里出现了三个容易混淆的名字，务必分清：

| 名字 | 出现位置 | 含义 |
| --- | --- | --- |
| `IBusRimeEngine` | rime_engine.c 里的结构体 | C 语言里的实际引擎类型 |
| `IBUS_TYPE_RIME_ENGINE` | rime_engine.h 的宏 | 上述类型对应的 GType，用于 GObject 系统 |
| `"rime"`（字符串） | `ibus_factory_add_engine` 第二参数 | 注册到工厂里的「引擎名」，供守护进程按名字查找 |

#### 4.2.2 核心流程

1. **`ibus_factory_new(ibus_bus_get_connection(bus))`**：用 `bus` 的底层 D-Bus 连接创建一个工厂。注意这里传的不是 `bus` 本身，而是从它取出的 `GDBusConnection`（通过 `ibus_bus_get_connection`）。
2. **`g_object_ref_sink(factory)`**：和上一节同理，把工厂的浮动引用转成拥有引用。
3. **`ibus_factory_add_engine(factory, "rime", IBUS_TYPE_RIME_ENGINE)`**：把名字 `"rime"` 与 GType `IBUS_TYPE_RIME_ENGINE` 绑定，登记到工厂的映射表里。

#### 4.2.3 源码精读

工厂创建与引擎注册只有三行（[rime_main.c:106-109](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L106-L109)）：

```c
IBusFactory *factory = ibus_factory_new(ibus_bus_get_connection(bus));
g_object_ref_sink(factory);

ibus_factory_add_engine(factory, "rime", IBUS_TYPE_RIME_ENGINE);
```

要点：

- `ibus_bus_get_connection(bus)` 把 `IBusBus` 包装的底层 `GDBusConnection` 取出来交给工厂。工厂后续通过这条连接向守护进程暴露自己。
- `IBUS_TYPE_RIME_ENGINE` 是一个宏，定义在 [rime_engine.h:6-7](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.h#L6-L7)：

  ```c
  #define IBUS_TYPE_RIME_ENGINE \
          (ibus_rime_engine_get_type())
  ```

  它展开后调用 `ibus_rime_engine_get_type()`，这个函数由 GObject 的 `G_DEFINE_TYPE` 宏自动生成，见 [rime_engine.c:67](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L67)：

  ```c
  G_DEFINE_TYPE (IBusRimeEngine, ibus_rime_engine, IBUS_TYPE_ENGINE)
  ```

  这一行的作用是：向 GObject 类型系统注册一个名为 `IBusRimeEngine` 的新类型，父类是 `IBus_TYPE_ENGINE`，并自动生成 `ibus_rime_engine_get_type()` 等一组辅助函数。**第一次调用 `IBUS_TYPE_RIME_ENGINE` 时，类型才会被真正注册（惰性注册）**，所以这一步既「取类型」也「触发注册」。

- 字符串 `"rime"` 是登记到工厂里的引擎名。这个名字必须和 `rime.xml.in` 里 `<engine>` 段的 `<name>` 完全一致（见 [rime.xml.in:13-14](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L13-L14)）：

  ```xml
  <engine>
      <name>rime</name>
  ```

  ibus 守护进程启动时先读 `rime.xml.in`（编译后是 `rime.xml`），知道这个组件提供一个名叫 `rime` 的引擎；运行时当用户选中 Rime，守护进程就按名字 `rime` 找到本进程的工厂，工厂再按映射表造出一个 `IBusRimeEngine` 实例。**XML 声明与代码注册两端必须对得上**，否则守护进程找不到引擎。

> 关于 `IBusRimeEngine` 这个类型本身的内部结构（它持有哪些成员、虚函数如何挂载），属于 u3-l1 的内容，本讲只关心它「被注册」这一步。

#### 4.2.4 代码实践

**实践目标：** 把「引擎名」在 XML 和代码两端的对应关系亲手对一遍。

**操作步骤：**

1. 打开 [rime.xml.in:12-25](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L12-L25)，找到 `<engine>` 段，记下 `<name>` 的值。
2. 打开 [rime_main.c:109](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L109)，找到 `ibus_factory_add_engine` 的第二个参数。
3. 做一个思想实验：如果把代码里的 `"rime"` 改成 `"Rime"`（大写 R），但 XML 不改，会发生什么？

**需要观察的现象：**

- 代码端字符串与 XML 端 `<name>` 现在是否完全相同？
- 修改后，ibus 守护进程能否找到引擎？

**预期结果：** 两端当前都是小写 `rime`，完全一致。若只改一端导致不一致，守护进程按 XML 里的名字 `rime` 来要引擎时，工厂里只有 `Rime` 这一项，于是无法创建实例，Rime 输入法将无法被激活——这印证了「两端必须严格对应」。

> 这是一个「源码阅读型实践」，无需真正编译运行，靠对照阅读即可完成。

#### 4.2.5 小练习与答案

**练习 1：** `ibus_factory_new` 的参数为什么是 `ibus_bus_get_connection(bus)`，而不是直接传 `bus`？

**参考答案：** 工厂需要一个底层的 `GDBusConnection` 来向守护进程暴露自己，而 `IBusBus` 是对这个连接以及一组高层操作的封装。`ibus_bus_get_connection` 正是用来取出那条底层连接，所以传它而不是整个 `bus` 对象。

**练习 2：** `IBUS_TYPE_RIME_ENGINE` 这个宏是在哪一行被「真正展开并触发类型注册」的？为什么不是在 `G_DEFINE_TYPE` 那一行？

**参考答案：** 它在 [rime_main.c:109](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L109) 的 `ibus_factory_add_engine` 调用处第一次被求值。`G_DEFINE_TYPE`（[rime_engine.c:67](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L67)）只是**定义**了 `ibus_rime_engine_get_type()` 这个函数（带线程安全的惰性初始化逻辑），类型的实际注册发生在该函数**第一次被调用**时——也就是宏第一次展开时。

---

### 4.3 request_name：在 D-Bus 上认领总线名

#### 4.3.1 概念说明

到目前为止，ibus-rime 已经连上了守护进程，也建好了能造引擎的工厂。但守护进程还需要一个**稳定的「门牌号」**才能在需要时找到本进程——这个门牌号就是 D-Bus 上的「总线名」。

`ibus_bus_request_name(bus, "im.rime.Rime", 0)` 的作用就是向 D-Bus 守护进程申请：请把 `im.rime.Rime` 这个众所周知的名字分配给我这条连接。申请成功后，任何通过这个名字寻址的消息都会路由到本进程。

这个名字必须和 `rime.xml.in` 里 `<component>` 段的 `<name>` 一致（[rime.xml.in:3-4](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L3-L4)）：

```xml
<component>
    <name>im.rime.Rime</name>
```

> 注意区分两个命名空间：`im.rime.Rime` 是**组件/总线名**（component name，用于 D-Bus 寻址），而 `rime` 是**引擎名**（engine name，用于工厂造引擎）。两者完全不同，不能混用。

#### 4.3.2 核心流程

1. 调用 `ibus_bus_request_name(bus, "im.rime.Rime", 0)`。
2. 若成功，本进程正式成为 `im.rime.Rime` 这个名字的持有者，守护进程可以按名字路由请求。
3. 若失败，调用 `g_error("error requesting bus name")` 打印致命错误日志，然后 `exit(1)` 终止进程。

#### 4.3.3 源码精读

申请总线名的代码（[rime_main.c:110-113](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L110-L113)）：

```c
if (!ibus_bus_request_name(bus, "im.rime.Rime", 0)) {
  g_error("error requesting bus name");
  exit(1);
}
```

要点：

- 第一个参数 `bus`：在哪条连接上申请。
- 第二个参数 `"im.rime.Rime"`：要申请的名字，必须与 `rime.xml.in` 的组件名一致。
- 第三个参数 `0`：D-Bus 的 name request 标志位，传 0 表示不附加任何特殊选项（不带 `DBUS_NAME_FLAG_DO_NOT_QUEUE` 之类）。即按默认语义申请。
- 返回值：成功返回非零（真），失败返回 0（假）。注意这里 `ibus_bus_request_name` 的布尔化返回值——成功为真，失败为假。

**为什么失败要 `exit(1)`？** 这是本讲最需要想清楚的一点：

- 申请失败通常意味着这个名字**已经被别的进程占用了**（比如已经有一个 ibus-engine-rime 在跑），或者发生了 D-Bus 权限问题。
- 此时进程虽然连上了守护进程，但拿不到名字，守护进程永远无法按 `im.rime.Rime` 找到它——也就是说这个进程**彻底没有用处了**。继续运行只会白白占用资源，还可能和已存在的实例互相干扰。
- 因此这是不可恢复的致命错误，用 `g_error`（GLib 的致命日志，默认会 `abort`/终止）记录后 `exit(1)`，让系统/用户看到出错信息。退出码 1 表示「错误退出」，与 4.1 节中 `exit(0)` 的「无事可做」形成对照。

为方便对照，把本讲两个退出点的区别列成表：

| 退出点 | 触发条件 | 退出码 | 含义 |
| --- | --- | --- | --- |
| [rime_main.c:99-102](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L99-L102) | `ibus_bus_is_connected` 为假 | `0` | 没有 ibus 环境，优雅退出 |
| [rime_main.c:110-113](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L110-L113) | `ibus_bus_request_name` 失败 | `1` | 连上了却拿不到名字，致命错误 |

申请成功之后，函数继续往下走：初始化 libnotify、设置 librime 的通知回调、`rime_api->setup()`、`ibus_rime_start()`、`ibus_rime_load_settings()`，最后进入 `ibus_main()` 阻塞主循环（[rime_main.c:115-129](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L115-L129)）。这些属于 u2-l3 的范围，本讲不展开。

#### 4.3.4 代码实践

**实践目标：** 验证总线名在 XML 与代码两端的对应，并理解失败处理为何是致命的。

**操作步骤：**

1. 打开 [rime.xml.in:3-4](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L3-L4)，确认组件名。
2. 打开 [rime_main.c:110](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L110)，确认 `request_name` 的第二个参数。
3. 思考：如果系统里已经有一个 ibus-engine-rime 在运行，再手动启动第二个，第二个会在哪一行退出？退出码是多少？
4. 进一步思考：能否把 `exit(1)` 改成「重试几次再退出」？这种改造在本场景下有没有意义？

**需要观察的现象：**

- 组件名 `im.rime.Rime` 与代码里 `request_name` 的名字是否完全一致？
- 第二个实例的退出位置和退出码。

**预期结果：** 两端都是 `im.rime.Rime`，完全一致。第二个实例会在 `request_name` 处失败，命中 `g_error("error requesting bus name")` 后 `exit(1)`。至于重试：因为名字已被合法占用（旧实例仍在工作），重试通常也拿不到，意义不大，所以原代码选择直接致命退出，把「同一时间只应有一个实例」这条不变量交给 D-Bus 命名机制来保证。

> 第 3、4 步的运行验证依赖一个真实的 IBus 桌面环境，若无条件复现请记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1：** `ibus_bus_request_name` 的第三个参数 `0` 表示什么？如果传非 0 会怎样？

**参考答案：** 第三个参数是 D-Bus 的 name request 标志位，0 表示不带任何特殊标志（既不要求「不排队」，也不要求「允许替换现有持有者」等）。若传非 0，会改变申请语义，例如让申请进入队列等待、或尝试抢占已有持有者。ibus-rime 这里用 0，即按默认语义直接申请。

**练习 2：** 组件名 `im.rime.Rime` 和引擎名 `rime` 为什么不能写成同一个？

**参考答案：** 它们处在两个不同的命名空间：组件名是 D-Bus 总线上的进程门牌号，用于寻址到「这个进程」；引擎名是工厂里登记的类型别名，用于在进程内部「造哪个引擎」。二者用途不同，IBus 也要求组件名符合 D-Bus 名字规范（用点分隔，类似反向域名），所以它们是两套不同的字符串，只是恰好都含 rime 这个词。

## 5. 综合实践

把本讲三步串起来，完成下面这个「端到端对照」小任务：

1. **画一张接入时序图。** 横轴是「ibus 守护进程」和「ibus-engine-rime 进程」两个角色，按时间顺序画出：进程启动 → `ibus_init` → `ibus_bus_new` 建连 → `is_connected` 检查 → `factory_new` + `add_engine` → `request_name` → 进入 `ibus_main` 主循环。在每个箭头旁标注对应的源码行号。

2. **做一张「名字对照表」。** 列出本讲涉及的所有名字字符串，分别填上它在 `rime.xml.in` 里的位置、在 `rime_main.c` 里的位置，以及它的作用。至少应包含：
   - 组件名 / 总线名 `im.rime.Rime`
   - 引擎名 `rime`
   - 可执行文件名 `ibus-engine-rime`（提示：见 [rime.xml.in:6](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime.xml.in#L6) 的 `<exec>`）

3. **回答两个关键问题（用本讲的源码佐证）：**
   - 为什么 `bus` 和 `factory` 都要 `g_object_ref_sink`，而最后又都要 `g_object_unref`？
   - 为什么 `request_name` 失败用 `exit(1)`、而 `is_connected` 失败用 `exit(0)`？

完成这张图和表后，你应当能够不看源码，复述出 ibus-rime 从启动到进入主循环、把自己「注册」给 IBus 框架的完整链路。

## 6. 本讲小结

- `rime_with_ibus()` 是 IBus 接入的主函数，按「连接 → 建工厂注册引擎 → 认领总线名 → 进主循环」推进。
- 连接三件套：`ibus_init()` 初始化库、`ibus_bus_new()` 建立到 ibus 守护进程的 D-Bus 连接、`ibus_bus_is_connected()` 确认连接可用，连不上则 `exit(0)` 优雅退出。
- `g_object_ref_sink` 把 IBus 对象的浮动引用转成明确的拥有引用，与函数末尾的 `g_object_unref` 成对，确保 `ibus_main()` 阻塞期间对象不被回收、退出时干净释放。
- `ibus_factory_new` + `ibus_factory_add_engine(factory, "rime", IBUS_TYPE_RIME_ENGINE)` 建立引擎工厂，把引擎名 `"rime"` 与 GObject 类型绑定；`IBUS_TYPE_RIME_ENGINE` 由 `G_DEFINE_TYPE` 惰性注册。
- `ibus_bus_request_name(bus, "im.rime.Rime", 0)` 在 D-Bus 上认领总线名，失败则 `exit(1)` 致命退出——因为拿不到名字进程就毫无用处。
- 必须分清两个命名空间：组件/总线名 `im.rime.Rime`（对应 `rime.xml.in` 的 `<component><name>`）与引擎名 `rime`（对应 `<engine><name>` 与 `add_engine` 的字符串），两端各自必须严格一致。
- `"disconnected"` 信号回调通过 `ibus_quit()` 让主循环退出，把断连处理纳入正常的收尾路径。

## 7. 下一步学习建议

本讲讲完了「接入 IBus」这一半。`rime_with_ibus()` 的另一半——librime 的 `setup` / `initialize` / `start_maintenance` / `deploy_config_file`，以及 `notification_handler` 如何把 librime 的部署消息转成桌面通知——正是下一讲 **u2-l3「librime 初始化、部署与通知」** 的内容，建议紧接着读。

读完 U2 之后，建议进入 **U3「引擎对象与会话机制」**：u3-l1 会钻进 `IBusRimeEngine` 这个 GObject 子类的内部（`class_init` / `init` / `destroy` 三件套、它持有的 session/status/table/props 成员），也就是本讲里被工厂「造出来」的那个引擎实例的真面目。届时你会看到，本讲的「注册」和 u3 的「被实例化」正好首尾相接。
