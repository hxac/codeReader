# IBusRimeEngine 类型与生命周期

## 1. 本讲目标

上一讲（u2-l2）我们在 `rime_with_ibus()` 里看到过这么一行：

```c
ibus_factory_add_engine(factory, "rime", IBUS_TYPE_RIME_ENGINE);
```

它把引擎名 `"rime"` 绑定到一个叫 `IBUS_TYPE_RIME_ENGINE` 的「类型把手」上。从这一讲开始，我们终于要打开这个类型本身，看清 **「一个 Rime 引擎实例」在 C 代码里到底长什么样、怎么被造出来、又怎么被销毁**。

`IBusRimeEngine` 是一个用 GObject 类型系统定义的「类」。虽然 C 语言没有 class 关键字，但 GLib 的 GObject 机制用一套宏 + 结构体约定，在 C 里模拟出了「类、继承、虚函数、构造/析构」的面向对象能力。ibus-rime 的全部业务——按键处理、候选词渲染、状态栏——都挂在这个类的虚函数和成员上。

学完本讲你应当能够：

- 说清楚 GObject 类型系统的基本运作方式（类型注册、惰性初始化、`get_type` 的角色）；
- 说清楚 `G_DEFINE_TYPE` 这一行宏替你生成了哪些东西，以及它为什么只在「第一次被用到」时才真正注册类型；
- 读懂 `class_init` 里如何把一组 C 函数「挂」到 `IBusEngineClass` 的虚函数槽位上；
- 解释 `init`（分配资源）与 `destroy`（释放资源）为什么是一对镜像，以及 `destroy` 里为什么要按特定顺序释放 `session / status / table / props` 再回调父类。

## 2. 前置知识

本讲要用到几个概念，先用通俗的话过一遍。

- **GObject 与类型系统。** GLib 提供了一套运行时类型系统（GType）。每个「类」在运行时都有一个唯一的 `GType` 标识（一个数字），以及一张记录了父类、实例大小、一组初始化函数的「类型信息表」。创建对象时，类型系统会按这张表分配内存、调用初始化函数。GObject 是这套系统里最常用的「带引用计数的对象基类」。
- **虚函数表（vtable）。** 在面向对象语言里，子类可以「重写」父类的方法。GObject 的做法是：每个类有一张函数指针表（class struct），父类把每个可重写的方法写成一个**函数指针成员**；子类在自己的 `class_init` 里把这些指针改写成自己的函数，就完成了「重写」。运行时通过指针调用，自然就调到了子类的实现。
- **`IBusEngine` 是 IBus 提供的引擎基类。** 它已经把「一个输入法引擎该有的行为」抽象成一堆虚函数：`process_key_event`（来按键了）、`focus_in`（输入框获得焦点）、`reset`（重置）、`property_activate`（点了状态栏按钮）等等。`IBusRimeEngine` 继承它，挑自己关心的几个虚函数重写。
- **引用计数与浮动引用。** 这是上一讲（u2-l2）讲过的概念：GObject 用引用计数管理生命周期，`ref` +1、`unref` -1，归零销毁；新创建的 `GInitiallyUnowned` 派生对象会带一个「浮动引用」，谁先 `g_object_ref_sink` 谁就把它「认领」成正常引用。本讲里 `init` 会再次用到 `g_object_ref_sink`。
- **`rime_api` 全局指针。** 这是 u2-l1 讲过的、指向 librime 的 `RimeApi` 句柄。引擎层通过它调用核心引擎的能力（建会话、销毁会话等）。

如果你对 `rime_api`、`IBUS_TYPE_RIME_ENGINE` 宏、IBus 工厂注册还不熟，建议先看 u2-l1 与 u2-l2。

## 3. 本讲源码地图

本讲只涉及两个文件，而且都集中在文件开头的「类型定义 + 三件套」部分：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [rime_engine.h](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.h) | 引擎类型对外声明 | 只暴露 `IBUS_TYPE_RIME_ENGINE` 宏和 `get_type` 声明 |
| [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) | 引擎类型的完整定义与实现 | 结构体、`G_DEFINE_TYPE`、`class_init`、`init`、`destroy` |

辅助理解还会顺带提到入口层的一行：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [rime_main.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c) | 进程入口层 | 第 109 行 `add_engine(..., IBUS_TYPE_RIME_ENGINE)` 触发类型注册 |

本讲**不**展开具体虚函数（按键处理、状态栏、候选表）的内部逻辑，那些分别属于 u3-l2、u3-l3、u4。本讲只回答：**这个类是怎么被「定义出来并登记进类型系统」的，一个实例从无到有、从有到无经历了什么。**

## 4. 核心概念与源码讲解

按规格，本讲拆成四个最小模块，正好对应一条完整的类型生命周期主线：

1. **GObject 类型系统**——类型怎么被登记、`get_type` 干什么；
2. **`G_DEFINE_TYPE` 宏**——一行宏替你省掉了多少样板代码；
3. **`class_init` 与 `IBusEngineClass` 虚函数表**——重写父类方法的地方；
4. **`init` / `destroy` 资源管理**——每个实例的「出生证」与「死亡证明」。

### 4.1 GObject 类型系统

#### 4.1.1 概念说明

C 语言本身没有「类」。要在 C 里写出「`IBusRimeEngine` 是 `IBusEngine` 的子类」这种关系，靠的是 GLib 的 **GType 类型系统**。

类型系统的核心思想：**每个类在运行时注册一次，拿到一个唯一的 `GType` 编号；之后每次创建该类的对象，类型系统都按注册时填写的「规格表」来分配内存、调用初始化函数。**

这张「规格表」至少包含：

- 类型名字符串（如 `"IBusRimeEngine"`）；
- 父类型 `GType`（这里是 `IBUS_TYPE_ENGINE`）；
- 类结构体大小与 `class_init` 函数（初始化虚函数表）；
- 实例结构体大小与 `instance_init` 函数（初始化每个对象）。

注册这件事必须是**幂等且线程安全**的：同一个类型无论被「请求」多少次，只能注册一次，之后都返回同一个 `GType`。GObject 用「惰性注册」实现这点——直到第一次有人调用 `xxx_get_type()` 时，才真正执行注册。

#### 4.1.2 核心流程

一个 GObject 子类从「被定义」到「能被实例化」，经历以下步骤：

1. **写宏与声明**：在头文件里写一个 `XXX_TYPE_YYY` 宏和一个 `xxx_yyy_get_type()` 声明。
2. **第一次取类型**：某处代码第一次用到 `XXX_TYPE_YYY` 宏（展开为 `xxx_yyy_get_type()`）。
3. **惰性注册**：`get_type` 内部发现类型还没注册，就调用 `g_type_register_static_simple`，把类型名、父类、`class_init`、`instance_init` 等填进类型系统，拿到 `GType` 返回。
4. **`class_init` 被调一次**：类型系统在注册时调用一次 `class_init`，初始化**类级别**的状态（主要是虚函数表）——注意这是「每类一次」，不是「每对象一次」。
5. **实例化**：之后每次 `g_object_new(XXX_TYPE_YYY, ...)`，类型系统分配实例内存、调用 `instance_init`（每对象一次），返回新对象。

对应到本项目，第 2 步就发生在入口层的那一行 `add_engine`：

[文件 rime_main.c:109](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L109-L109) —— 把 `"rime"` 绑定到 `IBUS_TYPE_RIME_ENGINE`，这是全程序**第一次**展开这个宏，也就触发了类型的真正注册。

#### 4.1.3 源码精读

头文件只暴露两样东西：一个宏、一个 `get_type` 声明。

[文件 rime_engine.h:6-9](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.h#L6-L9) —— 定义 `IBUS_TYPE_RIME_ENGINE` 宏，它展开后调用 `ibus_rime_engine_get_type()`：

```c
#define IBUS_TYPE_RIME_ENGINE \
        (ibus_rime_engine_get_type())

GType ibus_rime_engine_get_type();
```

注意 `ibus_rime_engine_get_type()` 的**函数体不是手写的**，而是由下一节要讲的 `G_DEFINE_TYPE` 宏自动生成。头文件只负责「声明」它，让别的 `.c` 文件（比如 `rime_main.c`）能调用。

这种「宏包函数」的写法是 GObject 的统一约定：任何用到该类型的地方写 `IBUS_TYPE_RIME_ENGINE`，预处理器就把它变成一次 `get_type()` 调用，从而「顺便」保证类型已注册。

#### 4.1.4 代码实践

**实践目标：** 验证「类型注册发生在第一次用到 `IBUS_TYPE_RIME_ENGINE` 时」。

**操作步骤：**

1. 用 `grep` 在整个仓库搜索 `IBUS_TYPE_RIME_ENGINE` 的所有出现位置。
2. 注意区分两类用法：**声明/定义处**（`rime_engine.h` 的宏定义、`rime_engine.c` 的 `G_DEFINE_TYPE`）与**使用处**（`rime_main.c:109` 的 `add_engine`）。
3. 思考：如果把 `rime_main.c:109` 整行删掉（假设性地），`IBusRimeEngine` 这个类型还会被注册吗？

**预期结果：** 真正触发 `get_type`、从而触发注册的，是 `rime_main.c:109` 这处**使用**；宏定义和 `G_DEFINE_TYPE` 本身只是「准备好能被注册」，并不会自己执行注册。

**待本地验证：** 若你想亲眼看到注册时机，可以在 `class_init` 函数体里临时加一行 `g_message("IBusRimeEngine class_init");`，重新编译后用 `ibus restart` 并观察日志（`journalctl` 或 `~/.local/share/ibus-rime/` 相关输出），应能看到这条日志在进程启动、IBus 首次需要引擎实例时各打印一次。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `IBUS_TYPE_RIME_ENGINE` 要写成「宏包函数」的形式，而不是直接用一个全局 `GType` 变量？

**参考答案：** 因为 `GType` 的值要到运行时注册后才确定，无法在编译期写死成常量。写成宏后，每次用到都会调用一次 `get_type()`，而 `get_type` 内部有「已注册就直接返回、未注册才注册」的判断，既保证了惰性注册，又对使用者透明——写 `IBUS_TYPE_RIME_ENGINE` 就像在用一个常量。

**练习 2：** `class_init` 是「每类一次」还是「每对象一次」？它和 `instance_init` 的调用次数有什么区别？

**参考答案：** `class_init` 每个类型只调用一次（注册时），用于初始化类级别的共享状态（虚函数表）；`instance_init`（本项目中叫 `ibus_rime_engine_init`）每创建一个对象调用一次，用于初始化实例成员。本项目中无论用户切换多少次 Rime 引擎，`class_init` 都只跑一次，而 `init` 会为每个引擎实例各跑一次。

### 4.2 G_DEFINE_TYPE 宏

#### 4.2.1 概念说明

如果手写一个 GObject 子类，`get_type()` 函数体是一大段枯燥的样板：定义一个静态的父类指针、写一个线程安全的「是否已注册」判断、填一张 `GTypeInfo` 结构体、调用 `g_type_register_static_simple`……每个类都长得几乎一样。

`G_DEFINE_TYPE` 就是 GLib 提供的「语法糖」宏，专门用来消除这些样板。**你只要告诉它三件事，它就替你生成全套注册代码。**

#### 4.2.2 核心流程

`G_DEFINE_TYPE` 接受三个参数（位置固定）：

- 第 1 个：**类名**（CamelCase，如 `IBusRimeEngine`）；
- 第 2 个：**函数名前缀**（小写下划线，如 `ibus_rime_engine`）；
- 第 3 个：**父类型的 `GType`**（如 `IBUS_TYPE_ENGINE`）。

这个宏会自动生成：

1. 一个静态变量 `ibus_rime_engine_parent_class`，指向父类的类结构（用于「回调父类」）；
2. `ibus_rime_engine_get_type()` 的函数体（含线程安全的惰性注册逻辑）；
3. 一些辅助声明。

同时，**宏要求你自己实现两个函数**（名字由前缀推导，不能写错）：

- `<前缀>_class_init`（即 `ibus_rime_engine_class_init`）；
- `<前缀>_init`（即 `ibus_rime_engine_init`）。

如果你没实现这两个函数，链接器会报「未定义引用」错误。

#### 4.2.3 源码精读

本项目里的这一行就是全部：

[文件 rime_engine.c:67](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L67-L67) —— 注册 `IBusRimeEngine` 类型，父类为 `IBusEngine`：

```c
G_DEFINE_TYPE (IBusRimeEngine, ibus_rime_engine, IBUS_TYPE_ENGINE)
```

读完这一行，你就知道了三件事，不必去别处找：

- 类型叫 `IBusRimeEngine`，函数前缀 `ibus_rime_engine`；
- 它继承自 `IBusEngine`（即 IBus 的引擎基类）；
- 下面一定存在 `ibus_rime_engine_class_init` 和 `ibus_rime_engine_init` 两个函数。

还有个**隐含产物**很重要，后面 `destroy` 会用到：宏生成了一个名为 `ibus_rime_engine_parent_class` 的静态指针，指向父类 `IBusEngineClass`。任何想「回调父类同名方法」的代码，都通过这个指针拿到父类。

#### 4.2.4 代码实践

**实践目标：** 体会 `G_DEFINE_TYPE` 帮你省掉了多少手写代码。

**操作步骤：**

1. 阅读本项目 `rime_engine.c:67` 这一行。
2. 到 GLib 官方文档或头文件 `gobject/gtype.h` 里查 `G_DEFINE_TYPE` 的展开形式（搜索关键词 `G_DEFINE_TYPE`）。
3. 对比「手写 `get_type` + `GTypeInfo` + `g_type_register_static_simple`」与「一行 `G_DEFINE_TYPE`」。

**预期结果：** 你会发现手写版需要约 20～30 行重复模板，而 `G_DEFINE_TYPE` 把它们压缩成一行，且强制你以「前缀 + `_class_init` / `_init`」的固定命名来实现这两个回调。

**待本地验证：** 如果你想看到宏到底生成了什么，可以在编译时加 `-E` 只做预处理（`gcc -E` 或在 `build/` 里手动跑一次预处理器），在输出里搜索 `ibus_rime_engine_get_type` 的函数体。

#### 4.2.5 小练习与答案

**练习 1：** 如果把第 3 个参数从 `IBUS_TYPE_ENGINE` 改成别的类型（比如 `IBUS_TYPE_OBJECT`），会发生什么？为什么本讲说它是「继承关系」的关键？

**参考答案：** 第 3 个参数决定了父类，整个继承链由此而定。改成 `IBUS_TYPE_OBJECT` 意味着 `IBusRimeEngine` 不再继承 `IBusEngine`，那么 `class_init` 里对 `IBusEngineClass` 虚函数槽位的赋值就会类型不匹配、编译报错，IBus 也无法把它当引擎来调度。所以这个参数就是「我是谁的儿子」的声明。

**练习 2：** `ibus_rime_engine_parent_class` 这个变量你在源码里找不到它的定义，为什么它却能用？

**参考答案：** 它是 `G_DEFINE_TYPE` 宏在展开时自动生成的静态变量，定义被「藏」在宏展开的代码里。编译器看得到，所以能引用；但人眼直接读 `.c` 文件看不到。这也是宏「魔法」需要注意的一点：读 GObject 代码时要心里有数，某些符号来自宏展开。

### 4.3 class_init 与 IBusEngineClass 虚函数表

#### 4.3.1 概念说明

`class_init` 是「类初始化函数」，在类型注册时被调用**一次**，目的是**填好虚函数表**。

「虚函数表」可以理解成父类留下的一排**空槽位**：父类 `IBusEngine` 知道「一个引擎会被要求处理按键、获得焦点、重置……」，但它把每个动作的具体实现留成**函数指针**。子类在 `class_init` 里把这些指针改写成自己的函数，就完成了「这个动作我来处理」的声明；没改写的槽位，运行时就走父类的默认实现。

在 GObject 里，这张虚函数表就是**类结构体本身**（这里是 `IBusEngineClass`）——它的每个函数指针成员就是一个虚函数槽。

#### 4.3.2 核心流程

`class_init` 的固定套路（本项目一字不差地遵守）：

1. 把传入的 `klass` 指针按需 cast 成多个父类视角（`IBusObjectClass *`、`IBusEngineClass *`）；
2. 给「析构」槽位赋值（IBus 用 `IBusObjectClass->destroy` 作为析构钩子）；
3. 逐个给关心的引擎虚函数槽位赋值（`process_key_event`、`focus_in`、`reset` ……）；
4. 不关心的槽位**不赋值**，留给父类默认实现。

赋值完成后，这张表就被类型系统记住了；之后无论创建多少个 `IBusRimeEngine` 实例，它们**共用同一张表**。

#### 4.3.3 源码精读

先看两个结构体——它们就是「实例」与「类」的形状：

[文件 rime_engine.c:15-23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L15-L23) —— **实例结构体** `_IBusRimeEngine`，第一个成员必须是父类实例 `IBusEngine parent`，后面跟着本类自己的四个成员：

```c
struct _IBusRimeEngine {
  IBusEngine parent;

  /* members */
  RimeSessionId session_id;   // librime 会话句柄
  RimeStatus status;          // 引擎状态（值类型，内嵌）
  IBusLookupTable* table;     // 候选词表（GObject 指针）
  IBusPropList* props;        // 状态栏属性列表（GObject 指针）
};
```

> GObject 的强制约定：实例结构体的**第一个成员必须是父类实例**。这样把子类指针 cast 成父类指针时，内存布局天然兼容——这是 C 里实现「继承」的底层技巧。

[文件 rime_engine.c:25-27](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L25-L27) —— **类结构体**，本类没有新增「类级别」成员，所以只是空壳继承：

```c
struct _IBusRimeEngineClass {
  IBusEngineClass parent;
};
```

接着看 `class_init` 主体：

[文件 rime_engine.c:69-87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L69-L87) —— 拿到两个父类视角的指针，然后填虚函数表：

```c
IBusObjectClass *ibus_object_class = IBUS_OBJECT_CLASS (klass);
IBusEngineClass *engine_class = IBUS_ENGINE_CLASS (klass);

ibus_object_class->destroy = (IBusObjectDestroyFunc) ibus_rime_engine_destroy;

engine_class->process_key_event = ibus_rime_engine_process_key_event;
engine_class->focus_in  = ibus_rime_engine_focus_in;
engine_class->focus_out = ibus_rime_engine_focus_out;
engine_class->reset     = ibus_rime_engine_reset;
engine_class->enable    = ibus_rime_engine_enable;
engine_class->disable   = ibus_rime_engine_disable;
engine_class->property_activate   = ibus_rime_engine_property_activate;
engine_class->candidate_clicked   = ibus_rime_engine_candidate_clicked;
engine_class->page_up   = ibus_rime_engine_page_up;
engine_class->page_down = ibus_rime_engine_page_down;
```

这里有两个要点：

1. **`destroy` 挂在 `IBusObjectClass` 上，不是 `IBusEngineClass`。** IBus 的析构钩子由更上层的 `IBusObject` 提供，所以要单独 cast 一份。注意它和 GObject 自带的 `finalize`/`dispose` 不是一回事——IBus 用自己的 `destroy` 虚函数做资源释放，本项目就把清理逻辑写在这里。
2. **只重写了 10 个虚函数。** 文件开头 [rime_engine.c:29-66](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L29-L66) 声明的函数原型里，还有 `set_cursor_location`、`set_capabilities`、`cursor_up`、`cursor_down`、`property_show`、`property_hide` 等并没有在 `class_init` 里被挂载。它们是「已声明但未启用」的静态函数（甚至其中一个函数名 `ibus_engine_set_cursor_location` 还缺少了 `_rime_` 段，与命名约定不符）。它们不会被虚函数表调用，属于预留/死代码——这正是下一个实践的观察点。

#### 4.3.4 代码实践

**实践目标：** 列出 `class_init` 中真正挂载的所有虚函数，并找出「声明了却没挂载」的函数。

**操作步骤：**

1. 打开 [rime_engine.c:69-87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L69-L87)，逐行记录被赋值的 `engine_class->成员` 名字。
2. 再翻到 [rime_engine.c:29-66](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L29-L66)，把所有声明的 static 函数原型列出来。
3. 两份清单做差集：哪些函数声明了、却没出现在 `class_init` 的赋值里？

**预期结果：**

挂载的虚函数（共 10 个）：`process_key_event`、`focus_in`、`focus_out`、`reset`、`enable`、`disable`、`property_activate`、`candidate_clicked`、`page_up`、`page_down`，外加挂在 `IBusObjectClass` 上的 `destroy`。

声明却未挂载（共 6 个）：`set_cursor_location`、`set_capabilities`、`cursor_up`、`cursor_down`、`property_show`、`property_hide`。它们目前不会经由虚函数表被调用。

#### 4.3.5 小练习与答案

**练习 1：** 为什么实例结构体的第一个成员必须是父类 `IBusEngine parent`？如果把它挪到第二个位置会怎样？

**参考答案：** GObject 靠「父类在内存最前面」来让 `IBusRimeEngine*` 和 `IBusEngine*` 之间能无成本互转——指向子类的指针同时也就是合法的父类指针。挪到第二位后，内存布局错位，所有 IBus/GLib 内部对它的父类视角访问都会读到错误偏移，程序会崩溃。

**练习 2：** `class_init` 里为什么对 `destroy` 用了 `(IBusObjectDestroyFunc)` 强制转换？

**参考答案：** 父类槽位 `ibus_object_class->destroy` 期望的函数指针类型是 `IBusObjectDestroyFunc`（参数是 `IBusObject*`），而本项目实现的 `ibus_rime_engine_destroy` 参数是 `IBusRimeEngine*`。两者签名在 C 严格类型检查下不完全一致，所以用一个显式转换告诉编译器「我知道我在做什么」。这是 GObject 代码里常见的写法——前提是你确实保证了运行时传进来的对象就是子类对象。

### 4.4 init / destroy 资源管理

#### 4.4.1 概念说明

`init` 是**实例构造函数**：每创建一个 `IBusRimeEngine` 对象调用一次，负责把四个成员初始化好。`destroy` 是**析构函数**（IBus 风格）：对象销毁前调用，负责把 `init` 里分配的资源逐一归还。

这两个函数必须是**镜像关系**：`init` 里申请了什么，`destroy` 里就要对应释放什么；而且最好**顺序相反**（后申请的先释放），这是资源管理的通用纪律，能避免「A 还在用 B，B 却已被释放」的悬空依赖。

本节的另一个重点是 **`g_object_ref_sink`**：`table` 和 `props` 都是 `GInitiallyUnowned` 派生的 GObject，刚创建时带浮动引用。`init` 里用 `g_object_ref_sink` 把浮动引用「下沉」成本引擎持有的正常引用；相应地，`destroy` 里用 `g_object_unref` 归还。

#### 4.4.2 核心流程

**`init` 做的事（按代码顺序）：**

1. 调 `ibus_rime_create_session` 建一个 librime 会话，拿到 `session_id`，并按全局设置决定 `soft_cursor` 选项；
2. 用 `RIME_STRUCT_INIT` / `RIME_STRUCT_CLEAR` 把内嵌的 `RimeStatus status` 清零；
3. `ibus_lookup_table_new(9, 0, TRUE, FALSE)` 建候选表，再 `g_object_ref_sink` 认领它；
4. `ibus_prop_list_new()` 建属性列表，再 `g_object_ref_sink` 认领它；
5. 往属性列表里塞三个状态栏按钮：`InputMode`（中/英切换）、`deploy`（部署）、`sync`（同步）。

**`destroy` 做的事（按代码顺序，与 `init` 大致反向）：**

1. 若有 `session_id`，调 `rime_api->destroy_session` 销毁会话，并把它清零；
2. 释放 `status.schema_id`、`status.schema_name`（这俩是 `g_strdup` 拷出来的字符串）；
3. `RIME_STRUCT_CLEAR(status)` 清结构；
4. `g_object_unref(table)` 归还候选表，指针置 `NULL`；
5. `g_object_unref(props)` 归还属性列表，指针置 `NULL`；
6. 最后通过 `ibus_rime_engine_parent_class->destroy` **回调父类的析构**，把对象本身交给 IBus 完成最终销毁。

引用计数上，浮动引用的「下沉」可以这么记（`ref_count` 表示引用计数，`floating` 是否浮动）：

\[
\text{新建浮动对象: } ref\_count = 1,\ floating = \text{true}
\]

\[
\text{ref\_sink 之后: } ref\_count = 1,\ floating = \text{false}
\]

也就是说 `ref_sink` 并没有改变计数大小，只是把「浮动」状态翻成了「正常」——本引擎从此正式「拥有」这个对象，后续 `unref` 才能让计数归零、触发释放。

#### 4.4.3 源码精读

先看建会话的小助手，它被 `init` 和 `focus_in` 共用：

[文件 rime_engine.c:89-98](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L89-L98) —— 调 librime 建会话并设置 `soft_cursor` 选项：

```c
rime_engine->session_id = rime_api->create_session();
Bool inline_caret =
    g_ibus_rime_settings.embed_preedit_text &&
    g_ibus_rime_settings.preedit_style == PREEDIT_STYLE_COMPOSITION &&
    g_ibus_rime_settings.cursor_type == CURSOR_TYPE_INSERT;
rime_api->set_option(rime_engine->session_id, "soft_cursor", !inline_caret);
```

注意它依赖全局 `g_ibus_rime_settings`——这正是 u5-l1 要讲的配置加载产物，本讲只需知道「会话建立时会读一次全局样式设置」。

再看 `init` 主体，重点看四个成员如何被分配与「认领」：

[文件 rime_engine.c:100-113](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L100-L113) —— 建会话、清状态、建表与属性列表并 sink：

```c
ibus_rime_create_session(rime_engine);

RIME_STRUCT_INIT(RimeStatus, rime_engine->status);
RIME_STRUCT_CLEAR(rime_engine->status);

rime_engine->table = ibus_lookup_table_new(9, 0, TRUE, FALSE);
g_object_ref_sink(rime_engine->table);

rime_engine->props = ibus_prop_list_new();
g_object_ref_sink(rime_engine->props);
```

`ibus_lookup_table_new(9, 0, TRUE, FALSE)` 的第一个参数 `9` 是每页候选数（page_size）；`table` 与 `props` 都是 `GInitiallyUnowned` 派生对象，新建后立即 `g_object_ref_sink` 把浮动引用收归本引擎——和 u2-l2 对 `bus`、`factory` 的处理完全一致。

[文件 rime_engine.c:114-153](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L114-L153) —— 往 `props` 里追加三个状态栏按钮（`InputMode` / `deploy` / `sync`），每个都用 `ibus_property_new(...)` 建好后 `ibus_prop_list_append` 进列表。这部分是 u3-l3 状态栏操作的数据底座，本讲只指出「按钮是在 `init` 里就备好的」。

最后看 `destroy`，它就是 `init` 的「反向操作」：

[文件 rime_engine.c:155-183](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L155-L183) —— 逐项归还资源，最后回调父类析构：

```c
if (rime_engine->session_id) {
  rime_api->destroy_session(rime_engine->session_id);
  rime_engine->session_id = 0;
}
if (rime_engine->status.schema_id) {
  g_free(rime_engine->status.schema_id);
}
if (rime_engine->status.schema_name) {
  g_free(rime_engine->status.schema_name);
}
RIME_STRUCT_CLEAR(rime_engine->status);

if (rime_engine->table) {
  g_object_unref(rime_engine->table);
  rime_engine->table = NULL;
}
if (rime_engine->props) {
  g_object_unref(rime_engine->props);
  rime_engine->props = NULL;
}

((IBusObjectClass *) ibus_rime_engine_parent_class)->destroy(
    (IBusObject *)rime_engine);
```

这段集中体现了几个工程纪律：

- **先释放自己的资源，最后回调父类。** 如果先调父类 `destroy`，对象可能已被 IBus 视作「正在拆除」，再去读 `rime_engine->session_id` 等成员就有风险。先把自有资源处理干净，再把对象整体交出去。
- **顺序与 `init` 大致相反。** `init` 里最先建的是会话，`destroy` 里最先销毁的也是会话；`init` 里最后建的是 `props`，`destroy` 里靠后释放。
- **释放后立即置 `NULL` / 清零。** `session_id = 0`、`table = NULL`、`props = NULL`、`RIME_STRUCT_CLEAR(...)`，防止析构过程中某个回调再次访问已释放资源时拿到野指针（双重释放/重复析构的常见防线）。
- **字符串单独 `g_free`。** `status.schema_id` 和 `status.schema_name` 是 `ibus_rime_update_status` 里用 `g_strdup` 拷贝出来的（见 [rime_engine.c:247-248](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L247-L248)），引擎拥有它们，必须由引擎在析构时释放；而 `status` 结构体本身是**内嵌值**（不是指针），随实例内存一起由类型系统回收，所以只清内容、不单独 free。
- **每个释放都带判空。** 因为在某些路径下（比如 `disable` 已经把会话销毁过、`session_id` 已为 0），资源可能已经不在了，判空避免对空指针操作。

最后一行用宏自动生成的 `ibus_rime_engine_parent_class` 拿到父类，调它的 `destroy`——这就是「回调父类析构」的标准写法，和 4.2 节讲到的隐含产物对上了。

#### 4.4.4 代码实践

**实践目标：** 验证 `init` 与 `destroy` 的镜像关系，并解释释放顺序。

**操作步骤：**

1. 把 [rime_engine.c:100-113](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L100-L113) 的 `init` 与 [rime_engine.c:155-183](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L155-L183) 的 `destroy` 左右对照，画一张「申请 ↔ 释放」配对表。
2. 回答：`destroy` 为什么把「回调父类」放在最后？
3. 回答：为什么 `status` 用 `RIME_STRUCT_CLEAR` 而 `table`/`props` 用 `g_object_unref`？

**预期结果（配对表）：**

| `init` 中的动作 | `destroy` 中的对应动作 | 资源类型 |
| --- | --- | --- |
| `create_session` → `session_id` | `destroy_session` + `session_id = 0` | librime 会话句柄（外部资源） |
| `RIME_STRUCT_INIT/CLEAR(status)` | `g_free(schema_id/schema_name)` + `RIME_STRUCT_CLEAR` | 内嵌值结构 + 两个 owned 字符串 |
| `lookup_table_new` + `ref_sink(table)` | `g_object_unref(table)` + 置 `NULL` | GObject（引用计数） |
| `prop_list_new` + `ref_sink(props)` | `g_object_unref(props)` + 置 `NULL` | GObject（引用计数） |

**关于顺序的解释：** 自有资源（会话、字符串、表、属性列表）都依赖「对象内存还可用」才能安全访问；回调父类 `destroy` 后对象进入拆除流程，所以必须把自有资源先处理完。这与「后申请先释放」的镜像顺序一起，保证了不存在「A 还要用 B，B 已被释放」的悬空依赖。

**关于两种释放方式的区别：** `table`/`props` 是**指针**，指向独立分配的 GObject，靠引用计数管理，所以要 `unref`（计数归零才真正释放，本引擎释放后若别处还持有，对象仍存活）；`status` 是**内嵌在实例里的值结构**，没有独立分配，不能 `free`，只需清空其内部指针成员。

**待本地验证：** 若想确认析构顺序，可在 `destroy` 函数体里每个 `if` 块后临时加 `g_debug("destroy: <资源名> released");`，重新编译后重启 IBus 并切换几次输入法，观察日志中四类资源的释放先后与是否各只出现一次。

#### 4.4.5 小练习与答案

**练习 1：** 假设把 `destroy` 里「回调父类」那一行挪到函数最开头，会出什么问题？

**参考答案：** 父类 `destroy` 一旦先执行，对象就进入 IBus 的拆除流程，此后访问 `rime_engine->session_id`、`->table`、`->props` 都可能读到已失效或正在被拆除的状态；更糟的是，父类析构可能触发信号、释放相关内存，导致后面的 `destroy_session`、`g_object_unref` 操作野指针。正确做法永远是「先收拾自己的东西，再交给父类」。

**练习 2：** `destroy` 里为什么每个资源释放前都要 `if (rime_engine->xxx)` 判空？给一个具体场景。

**参考答案：** 因为资源可能在对象析构前就已经被中途释放过。例如 `ibus_rime_engine_disable`（[rime_engine.c:221-228](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L221-L228)）已经把 `session_id` 销毁并置 0；如果引擎随后再被析构，`destroy` 里对 `session_id` 的判空就能跳过这次重复销毁。`init` 里对 `table`/`props` 赋值后若某分支提前失败，同样需要靠判空避免对未初始化指针解引用。判空是双重释放/野指针的第一道防线。

**练习 3：** `init` 里建 `table` 和 `props` 之后都紧跟一句 `g_object_ref_sink`。如果漏掉这两句，会怎样？

**参考答案：** 这两个对象以浮动引用创建，若不 sink，没有任何人「正式持有」它们。后续一旦有代码 `unref`（包括 `destroy` 里的 `g_object_unref`），浮动引用的计数行为和正常引用不同，可能导致对象被过早销毁或在析构时计数对不上、引发泄漏或崩溃。`ref_sink` 的作用就是在本讲 4.4.2 的公式里把 `floating` 从 true 翻成 false，确立「本引擎拥有它」的明确语义。

## 5. 综合实践

把本讲四个最小模块串起来，做一个小扩展练习：**给 `IBusRimeEngine` 新增一个会被 `init` 分配、`destroy` 释放的成员，并验证它的生命周期。**

> 说明：本任务是「源码阅读 + 设计型实践」，不要求你真的修改并编译运行（那会动到源码，超出本讲范围）。请以**设计 + 在草稿上写补丁**的方式完成，重点是把四件套的协作关系想清楚。

**目标：** 假设我们想给引擎加一个「调试用时间戳」成员 `gdouble created_at_sec`（仅用于练习生命周期，无实际功能）。

**操作步骤：**

1. **改实例结构体**：在 [rime_engine.c:15-23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L15-L23) 的 `_IBusRimeEngine` 里新增一个成员（比如放在 `props` 后面）。
2. **在 `init` 里初始化它**：在 [rime_engine.c:100-113](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L100-L113) 末尾给它赋一个初值（例如固定写 `0.0`，或调用 `g_get_real_time()`）。
3. **在 `destroy` 里清理它**：如果是值类型（`gdouble`），无需 free，只需在 [rime_engine.c:155-183](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L155-L183) 里按需重置；如果改成指针类型（例如 `gchar *debug_tag = g_strdup(...)`），就要在 `destroy` 里加一段 `if (rime_engine->debug_tag) { g_free(rime_engine->debug_tag); rime_engine->debug_tag = NULL; }`，并思考它应该插在四类资源的哪一档。
4. **回答三个问题**：
   - 这个新成员的初始化应该排在 `init` 现有四步的哪个位置？为什么？
   - 它的释放应该排在 `destroy` 的哪个位置？是否需要在「回调父类」之前？
   - 如果它是 `g_strdup` 出来的指针，为什么不能像 `status` 那样只 `RIME_STRUCT_CLEAR`？

**预期结果：** 你应当能画出一条从「结构体声明 → `init` 分配 → 使用 → `destroy` 释放 → 回调父类」的完整生命周期闭环，并且能解释「值类型 vs 指针类型」「内嵌 vs 独立分配」「自有资源先于父类释放」这几条规则分别如何决定你的补丁写法。这正是后续给 ibus-rime 做任何引擎层扩展（新增状态、缓存、句柄）时必须遵守的范式。

## 6. 本讲小结

- `IBusRimeEngine` 是一个用 GObject 类型系统定义的类，继承自 `IBusEngine`；头文件 [rime_engine.h:6-9](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.h#L6-L9) 只暴露 `IBUS_TYPE_RIME_ENGINE` 宏与 `get_type` 声明。
- `G_DEFINE_TYPE`（[rime_engine.c:67](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L67)）这一行宏替项目生成了 `get_type`、`parent_class` 指针与惰性注册逻辑；类型在 `rime_main.c:109` 第一次用到宏时才真正注册。
- `class_init`（[rime_engine.c:69-87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L69-L87)）每类调一次，用于填虚函数表：重写了 10 个 `IBusEngineClass` 虚函数，并把 `destroy` 挂到 `IBusObjectClass` 上；另有约 6 个声明了的函数未被挂载。
- 实例结构体（[rime_engine.c:15-23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L15-L23)）持有 `session_id`、`status`、`table`、`props` 四个成员，由 `init`（[rime_engine.c:100-153](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L100-L153)）分配。
- `destroy`（[rime_engine.c:155-183](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L155-L183)）与 `init` 镜像、顺序相反，先逐项释放自有资源（会话、字符串、表、属性列表），最后才回调父类析构。
- 释放方式因资源而异：GObject 指针用 `g_object_unref`（配对 `init` 的 `g_object_ref_sink`），内嵌值结构用 `RIME_STRUCT_CLEAR`，owned 字符串用 `g_free`——这套纪律是给引擎层做任何扩展时的模板。

## 7. 下一步学习建议

本讲只搭好了「类型骨架」和「生灭周期」，还没碰任何一个虚函数的具体实现。建议接下来：

- **u3-l2 会话管理与按键处理主链路**：精读 `ibus_rime_engine_process_key_event`（[rime_engine.c:509-544](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L509-L544)），看按键如何从 IBus 转发到 librime，并理解 `session_id` 的 create/find/destroy 全生命周期。
- **u3-l3 引擎回调与状态栏操作**：精读 `focus_in`/`disable`/`reset`/`property_activate`，看本讲在 `init` 里备好的 `props` 三个按钮（`InputMode`/`deploy`/`sync`）是如何被点击触发的。
- **u4 前端 UI 渲染**：本讲里只是「申请下来」的 `table` 成员，将在 u4-l3 被填上候选词；`status` 成员将在 u4-l1 驱动状态栏图标切换。
- 若想再巩固 GObject 基础，可顺手阅读 GLib 官方手册中 `G_DEFINE_TYPE` 与 [GObject 内存管理](https://docs.gtk.org/gobject/concepts.html#memory-management) 一节，对照本讲的 `ref_sink` / `unref` 配对加深理解。
