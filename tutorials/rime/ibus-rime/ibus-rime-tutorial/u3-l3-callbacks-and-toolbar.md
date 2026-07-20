# 引擎回调与状态栏操作

## 1. 本讲目标

本讲接续 u3-l2「会话管理与按键处理主链路」，把视线从「按键如何进入 librime」转移到「IBus 框架在什么时候、用什么方式回调我们的引擎，以及状态栏上的三个按钮被点击后会发生什么」。

读完本讲，你应当能够：

- 说清 `focus_in` / `focus_out` / `reset` / `enable` / `disable` 五个生命周期回调各自的职责，以及为什么其中几个是空实现。
- 解释 `property_activate` 如何用字符串分发把一次按钮点击路由到正确的 librime 操作。
- 描述「中英文切换（InputMode）」按钮如何通过翻转 `ascii_mode` 会话选项来改变输入模式，并联动状态栏图标。
- 说明「部署（Deploy）」按钮为什么等价于「重启 librime 核心」，以及「同步（Sync）」按钮与后台维护线程之间微妙的竞态关系（依据源码注释）。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 IBus 的「虚函数表」回调模型

IBus 的引擎基类 `IBusEngineClass` 是一个带一组「虚函数指针」的结构体（C 里模拟面向对象的多态）。框架在不同时机调用这些函数，例如：

- 输入框获得/失去焦点 → `focus_in` / `focus_out`
- 应用请求重置输入法（例如切换输入框）→ `reset`
- 引擎被启用/禁用 → `enable` / `disable`
- 用户点击状态栏按钮 → `property_activate`

ibus-rime 在 `class_init` 里把感兴趣的虚函数指针替换成自己的实现，这一过程在 u3-l1 已经讲过。本讲只读这些实现本身。

### 2.2 状态栏按钮 = IBusProperty

IBus 的状态栏（IBus panel）上显示的每一个按钮都是一个 `IBusProperty` 对象，关键属性包括：

| 字段 | 含义 |
| --- | --- |
| `key`（创建时第一个参数） | 按钮的唯一标识，点击时作为 `prop_name` 回传 |
| `label` | 按钮文字（如「中文」「部署」） |
| `icon` | 图标路径 |
| `symbol` | 紧凑模式下显示的单字符符号 |
| `tips` | 鼠标悬停提示 |

按钮被点击时，IBus 框架会回调 `property_activate(engine, prop_name, prop_state)`，其中 `prop_name` 就是创建按钮时给的 `key`。所以「按钮身份」与「点击处理」之间靠这根字符串绑定，**两端必须严格一致**——这是本讲反复出现的主题。

## 3. 本讲源码地图

本讲几乎全部内容都在一个文件里：

| 文件 | 作用 |
| --- | --- |
| `rime_engine.c` | 引擎层。本讲的五个生命周期回调、`property_activate`、状态栏属性创建都在这里 |
| `rime_engine.h` | 只暴露 `IBUS_TYPE_RIME_ENGINE` 宏与 `get_type` 声明，本讲不深入 |
| `rime_main.c` | 入口层。`ibus_rime_start` / `ibus_rime_stop` 定义在这里，是「部署」按钮的核心依赖 |
| `rime_settings.h` | 全局设置结构 `g_ibus_rime_settings`，状态栏方向等读取来源 |

涉及的几个关键函数集中度很高，先给一张定位表（行号以当前 HEAD 为准）：

| 函数 | 位置 | 一句话职责 |
| --- | --- | --- |
| `ibus_rime_engine_class_init` | `rime_engine.c:L69-L87` | 挂载本讲所有回调到虚函数表 |
| `ibus_rime_engine_init`（属性创建段） | `rime_engine.c:L114-L152` | 创建三个状态栏按钮 |
| `ibus_rime_engine_focus_in` | `rime_engine.c:L185-L194` | 注册属性、找回会话、刷新 |
| `ibus_rime_engine_focus_out` | `rime_engine.c:L196-L199` | 空实现 |
| `ibus_rime_engine_reset` | `rime_engine.c:L201-L213` | 清空候选与预编辑 |
| `ibus_rime_engine_enable` | `rime_engine.c:L215-L218` | 空实现 |
| `ibus_rime_engine_disable` | `rime_engine.c:L220-L228` | 销毁会话 |
| `ibus_rime_update_status` | `rime_engine.c:L230-L281` | 状态栏图标/标签切换（被各回调间接调用） |
| `ibus_rime_engine_property_activate` | `rime_engine.c:L546-L575` | 三按钮分发 |
| `ibus_rime_start` | `rime_main.c:L66-L81` | 部署按钮的「重启核心」依赖 |
| `ibus_rime_stop` | `rime_main.c:L83-L87` | 部署按钮的「关闭核心」依赖 |

## 4. 核心概念与源码讲解

### 4.1 生命周期回调：focus_in / focus_out / reset / enable / disable

#### 4.1.1 概念说明

当用户在不同应用、不同输入框之间切换，或系统对输入法发出重置/启停指令时，IBus 框架会调用引擎上对应的虚函数。这些回调构成了引擎的「生命周期事件面」。

ibus-rime 对这五个事件的态度并不平均——它只在**真正需要做事**的事件里写逻辑，其余保持空函数体。这是一种刻意设计：作为薄前端，凡是 librime 自己能管好的状态，前端就不重复干预。

#### 4.1.2 核心流程

五个回调的职责可以浓缩成下面这张表：

| 回调 | 触发时机 | ibus-rime 做的事 | 为什么这样做 |
| --- | --- | --- | --- |
| `focus_in` | 输入框获得焦点 | 注册属性列表、找回/重建会话、刷新一次 UI | 焦点切换时 panel 状态不保留，必须重报按钮；会话可能已被 disable 销毁 |
| `focus_out` | 输入框失去焦点 | （空） | librime 会话与上下文需要保留，便于回到同一输入框时继续 |
| `reset` | 框架请求重置 | 清空 composition、清空 preedit、刷新 | 响应「放弃当前输入」的语义 |
| `enable` | 引擎被启用 | （空） | 会话在 `init` / `focus_in` 已就绪，无需重复 |
| `disable` | 引擎被禁用 | 销毁会话 | 释放 librime 资源，避免泄漏 |

注意 `focus_in` 与 `disable` 形成一对张力：`disable` 销毁会话，`focus_in` 必须能把会话找回来或重建。这正是 u3-l2 里强调「按键前先 `find_session`」的同一套防御思想。

#### 4.1.3 源码精读

先看 `focus_in`，它做的事最多：

[rime_engine.c:L185-L194](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L185-L194) —— 焦点进入时把状态栏按钮重新注册给 IBus panel，并在会话缺失时重建会话，最后刷新一次 UI。

```c
static void
ibus_rime_engine_focus_in (IBusEngine *engine)
{
  IBusRimeEngine *rime_engine = (IBusRimeEngine *)engine;
  ibus_engine_register_properties(engine, rime_engine->props);
  if (!rime_engine->session_id) {
    ibus_rime_create_session(rime_engine);
  }
  ibus_rime_engine_update(rime_engine);
}
```

三个动作各有来由：

- `ibus_engine_register_properties` 把 `init` 里建好的三个按钮（见 4.2.3）整体推给 panel。IBus 在焦点切换时**不会替你保留**上一次的属性列表，所以每次 `focus_in` 都要重报一次，否则状态栏会变空。
- `if (!rime_engine->session_id)` 是防御性找回：若上一个输入框 `disable` 过，会话已被销毁（`session_id` 置 0），这里补建一个。
- `ibus_rime_engine_update` 触发一次完整 UI 同步（u4-l1 会专题讲解），保证回到输入框时立刻看到正确的候选表与状态图标。

再看 `reset`，它代表「丢弃当前未提交的输入」：

[rime_engine.c:L201-L213](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L201-L213) —— 调 librime 的 `clear_composition` 清掉引擎内部的候选缓冲，再用一段空文本把内联预编辑区擦干净。

```c
static void
ibus_rime_engine_reset (IBusEngine *engine)
{
  IBusRimeEngine *rime_engine = (IBusRimeEngine *)engine;
  if (rime_engine->session_id) {
    rime_api->clear_composition(rime_engine->session_id);
    // Clear uncommited contents of the pre-edit buffer.
    ibus_engine_update_preedit_text_with_mode(
        engine, ibus_text_new_from_static_string(""), 0, FALSE, IBUS_ENGINE_PREEDIT_CLEAR);
    ibus_rime_engine_update(rime_engine);
  }
}
```

注意它先调用 librime 的 `clear_composition`（清引擎内部状态），再调 IBus 的 `ibus_engine_update_preedit_text_with_mode`（清前端显示）——两层各清一遍，因为前端显示的是 librime 状态的「投影」， librime 清了不代表屏幕自动跟着清。

最后看 `disable`，它和 u3-l1 讲过的 `destroy` 形成对照：

[rime_engine.c:L220-L228](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L220-L228) —— 引擎被禁用时销毁当前 librime 会话并置零句柄，但**不释放** `table`/`props`/`status`（它们随引擎对象本身存活到 `destroy`）。

```c
static void
ibus_rime_engine_disable (IBusEngine *engine)
{
  IBusRimeEngine *rime_engine = (IBusRimeEngine *)engine;
  if (rime_engine->session_id) {
    rime_api->destroy_session(rime_engine->session_id);
    rime_engine->session_id = 0;
  }
}
```

`disable` 与 `destroy` 的区别很关键：`disable` 只是「停用会话」，引擎对象还在，随时可能被 `focus_in` 重新激活；而 `destroy` 是引擎对象本身的析构（u3-l1），必须把所有成员连同对象一起释放。

至于 `focus_out` 与 `enable` 是空函数体：

[rime_engine.c:L196-L199](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L196-L199) 与 [rime_engine.c:L215-L218](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L215-L218) —— 两者均不做事，分别因为「离开焦点时要保留上下文」「启用时会话已由 init/focus_in 就绪」。

这些空实现并不是「TODO 待补」，而是经过权衡后的合理空缺。

#### 4.1.4 代码实践

**实践目标**：通过源码阅读，验证 `focus_in` 与 `disable` 的会话找回关系。

**操作步骤**：

1. 打开 `rime_engine.c`，定位 `disable`（L220）与 `focus_in`（L185）。
2. 在脑中（或在纸上）模拟下面这个时序，标注每一步 `rime_engine->session_id` 的值：
   - T1：引擎刚 `init`，`session_id` 被设为某非 0 值。
   - T2：用户切走，框架回调 `disable`。
   - T3：用户切回，框架回调 `focus_in`。
3. 跟进 `ibus_rime_create_session`（[rime_engine.c:L89-L98](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L89-L98)），确认它会调用 `rime_api->create_session()` 给 `session_id` 重新赋值。

**需要观察的现象**：T2 之后 `session_id` 应为 0；T3 的 `if (!rime_engine->session_id)` 判断为真，触发重建。

**预期结果**：你能画出一条「`disable` 清零 → `focus_in` 补建」的对称路径，并解释为什么 `focus_in` 不能假设会话一定存在。

> 由于本实践是源码阅读型，不需要真正运行程序，结论可直接从代码推导得出。

#### 4.1.5 小练习与答案

**练习 1**：`enable` 为什么是空实现？如果改成在那里 `create_session`，会和现在的代码产生什么重复？

**参考答案**：因为引擎对象的 `init`（[rime_engine.c:L101-L103](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L101-L103)）已经调用 `ibus_rime_create_session` 建好了会话，`focus_in` 又有找回逻辑兜底，`enable` 时会话几乎总是已就绪。若在 `enable` 里再建一次，会与 `init` 重复，且可能造成旧会话泄漏（旧 `session_id` 被直接覆盖而无 `destroy_session`）。

**练习 2**：`reset` 里为什么要同时调 `clear_composition` 和 `ibus_engine_update_preedit_text_with_mode(..., "")`？

**参考答案**：前者清的是 librime 引擎内部的 composition 缓冲（候选、拼音串），后者清的是 IBus 前端屏幕上的内联预编辑区。两者是「数据源」与「投影」的关系，必须分别清理，否则会出现「引擎已空、屏幕仍显示旧字」的不一致。

---

### 4.2 property_activate：属性激活与状态栏按钮分发

#### 4.2.1 概念说明

`property_activate` 是 `IBusEngineClass` 的虚函数，专门处理「用户点击了状态栏上的某个 `IBusProperty` 按钮」这一事件。它的签名是：

```c
void property_activate(IBusEngine *engine, const gchar *prop_name, guint prop_state);
```

- `prop_name`：被点击按钮的 `key`（创建时给的名字）。
- `prop_state`：按钮状态（对勾选型按钮有意义，本项目用 `PROP_TYPE_NORMAL` 故基本忽略）。

ibus-rime 的实现是一个典型的「字符串分发器」：用 `strcmp` 比较 `prop_name`，把三种按钮分别路由到 librime 的不同操作。

#### 4.2.2 核心流程

```text
property_activate(engine, prop_name, state)
        │
        ├── strcmp(prop_name, "deploy") == 0 ?
        │       └── 是 → ibus_rime_stop()
        │                 ibus_rime_start(TRUE)   ← 重启 librime，full_check=TRUE
        │                 ibus_rime_engine_update()
        │
        ├── strcmp(prop_name, "sync") == 0 ?
        │       └── 是 → rime_api->sync_user_data()
        │                 ibus_rime_engine_update()
        │
        ├── strcmp(prop_name, "InputMode") == 0 ?
        │       └── 是 → 翻转 ascii_mode 选项（见 4.3）
        │                 ibus_rime_engine_update()
        │
        └── 都不匹配 → 静默忽略
```

三个分支结构高度一致：**执行操作 → 调用 `ibus_rime_engine_update` 刷新 UI**。这是因为 librime 的状态在每次操作后都可能变化（schema 变了、模式变了、候选清空了），必须重新拉取并投影到 IBus。

注意三个 `prop_name` 字符串的大小写：`"deploy"`、`"sync"` 全小写，`"InputMode"` 驼峰。它们必须与按钮创建时的 `key` 严格一致，否则分发失败、按钮点击无响应。

#### 4.2.3 源码精读

先看按钮是怎么被创建的——三个按钮都在引擎 `init` 里依次构造，追加到同一个 `IBusPropList`：

[rime_engine.c:L114-L152](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L114-L152) —— 用 `ibus_property_new` 创建 InputMode / deploy / sync 三个按钮，每个按钮的第一个字符串参数就是它的身份标识（点击时回传的 `prop_name`）。

```c
// InputMode：中英文切换
prop = ibus_property_new("InputMode", PROP_TYPE_NORMAL, label,
                         IBUS_RIME_ICONS_DIR "/zh.png", tips,
                         TRUE, TRUE, PROP_STATE_UNCHECKED, NULL);
ibus_prop_list_append(rime_engine->props, prop);
// deploy：部署
prop = ibus_property_new("deploy", PROP_TYPE_NORMAL, label,
                         IBUS_RIME_ICONS_DIR "/reload.png", tips,
                         TRUE, TRUE, PROP_STATE_UNCHECKED, NULL);
ibus_prop_list_append(rime_engine->props, prop);
// sync：同步
prop = ibus_property_new("sync", PROP_TYPE_NORMAL, label,
                         IBUS_RIME_ICONS_DIR "/sync.png", tips,
                         TRUE, TRUE, PROP_STATE_UNCHECKED, NULL);
ibus_prop_list_append(rime_engine->props, prop);
```

这段代码揭示了三件事：

1. **`key` 与 `icon` 的对应**：InputMode→`zh.png`、deploy→`reload.png`、sync→`sync.png`，图标语义一目了然（这些 png 都真实存在于 `icons/` 目录）。
2. **`label`/`tips` 是静态字符串**：用 `ibus_text_new_from_static_string`，意味着文字常量不会被复制，引擎不持有其所有权。
3. **顺序很重要**：三个按钮按 `append` 顺序进入列表，索引依次为 0、1、2。后面 `ibus_rime_update_status` 会用 `ibus_prop_list_get(props, 0)` 取**第一个**（InputMode）来更新图标（见 4.3.3）。

再看分发器本身，用 `extern` 声明引入了入口层的两个函数（它们的定义在 `rime_main.c`，跨文件调用）：

[rime_engine.c:L546-L575](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L546-L575) —— 这是状态栏按钮的总入口，按 `prop_name` 字符串把点击事件分发到 deploy / sync / InputMode 三条处理路径。

```c
static void ibus_rime_engine_property_activate (IBusEngine *engine,
                                                const gchar *prop_name,
                                                guint prop_state)
{
  extern void ibus_rime_start(gboolean full_check);
  extern void ibus_rime_stop();
  IBusRimeEngine *rime_engine = (IBusRimeEngine *)engine;
  if (!strcmp("deploy", prop_name)) {
    ibus_rime_stop();
    ibus_rime_start(TRUE);
    ibus_rime_engine_update(rime_engine);
  }
  else if (!strcmp("sync", prop_name)) {
    // ... 见 4.4.3
    rime_api->sync_user_data();
    ibus_rime_engine_update(rime_engine);
  }
  else if (!strcmp("InputMode", prop_name)) {
    rime_api->set_option(
        rime_engine->session_id, "ascii_mode",
        !rime_api->get_option(rime_engine->session_id, "ascii_mode"));
    ibus_rime_engine_update(rime_engine);
  }
}
```

值得注意的工程细节：

- **`extern` 声明放在函数体内**：`ibus_rime_start` / `ibus_rime_stop` 定义在 `rime_main.c`，这里用函数内 `extern` 声明引入，是一种较老式但合法的写法，避免了修改头文件。
- **三条分支互斥、用 `else if` 串联**：保证一次点击只走一条路径。
- **三个字符串字面量的拼写必须与 `init` 里 `ibus_property_new` 的第一个参数逐字符相同**——大小写敏感。把这里的 `"InputMode"` 改成 `"inputmode"`，按钮就会失效。

#### 4.2.4 代码实践

**实践目标**：通过「改坏再改回」的对照阅读，验证 `key` 字符串两端一致的必要性。

**操作步骤**：

1. 在 [rime_engine.c:L114-L152](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L114-L152) 找到 `ibus_property_new("deploy", ...)`，记下这个 `key`。
2. 在 [rime_engine.c:L553](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L553) 找到 `if (!strcmp("deploy", prop_name))`，确认两端字符串一致。
3. **不要真的修改源码**（本任务禁止改源码），只在脑中假设：把 `init` 里的 `"deploy"` 改成 `"Deploy"`（首字母大写），点击「部署」按钮会怎样？

**需要观察的现象**：`strcmp("deploy", "Deploy")` 在 C 里返回非 0（不相等），因为 ASCII 中大写字母在小写字母之前。

**预期结果**：第一个 `if` 不命中，依次落到 `sync`、`InputMode` 分支，也都不命中，最终整次点击被静默忽略——按钮看起来「没反应」。由此体会字符串两端一致的硬约束。

> 本实践为源码阅读型，结论可由 `strcmp` 语义直接推出，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么三个分支最后都要调用一次 `ibus_rime_engine_update`？能不能只在 `InputMode` 分支里调？

**参考答案**：不能。三条路径都会改变 librime 的可观测状态——deploy 重启了核心（会话可能失效、schema 可能变）、sync 可能触发数据写入后的状态变化、InputMode 切换了模式图标。`update` 负责把 librime 最新状态重新投影到 IBus 的状态栏/候选表/预编辑区。漏调任一支都会导致 UI 与引擎状态不一致。

**练习 2**：`prop_state` 参数在 ibus-rime 里为什么基本被忽略？

**参考答案**：因为三个按钮创建时类型都是 `PROP_TYPE_NORMAL`（普通按钮），不是 `PROP_TYPE_TOGGLE`（勾选型）。`prop_state` 主要用于勾选型按钮表示「开/关」，普通按钮点击时该值无意义，所以代码不读取它。

---

### 4.3 InputMode：ascii_mode 中英文切换

#### 4.3.1 概念说明

`ascii_mode` 是 librime 的一个**会话级选项（option）**，决定当前会话处于「中文输入模式」还是「英文（ASCII）直接输出模式」。它是一个**用户可见的标准选项**——注意名字没有下划线前缀，这区别于 u3-l2 学过的内部选项 `_horizontal`（带前缀，前端专用）。

InputMode 按钮的本质，就是读取 `ascii_mode` 的当前值，取反后写回，从而在「中文」和「英文直出」之间切换。切换之后，状态栏图标会随之变化（中文模式显 `zh.png`，英文模式显 `abc.png`），让用户一眼看到当前状态。

#### 4.3.2 核心流程

```text
点击 InputMode 按钮
        │
        ▼
cur = rime_api->get_option(session, "ascii_mode")   // 读当前模式
        │
        ▼
rime_api->set_option(session, "ascii_mode", !cur)   // 写入相反值
        │
        ▼
ibus_rime_engine_update()
        │
        ├── get_status → 拿到最新的 is_ascii_mode
        └── ibus_rime_update_status → 换图标/标签
```

`get_option` / `set_option` 是 u3-l2 已经详细讲过的 librime 会话选项读写接口。这里用的是它们的第三种用法——读写一个**布尔型用户选项**。

#### 4.3.3 源码精读

切换逻辑只有三行，但很精炼：

[rime_engine.c:L569-L574](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L569-L574) —— 读出当前 `ascii_mode`，取反写回，从而在中英文模式间切换。

```c
  else if (!strcmp("InputMode", prop_name)) {
    rime_api->set_option(
        rime_engine->session_id, "ascii_mode",
        !rime_api->get_option(rime_engine->session_id, "ascii_mode"));
    ibus_rime_engine_update(rime_engine);
  }
```

注意 `!rime_api->get_option(...)` 这一处「读后立即取反写入」的紧凑写法：它把「读当前值」和「写反值」合并成一个表达式，等价于先 `Bool cur = get_option(...); set_option(..., !cur);`。这种写法依赖 `get_option` 没有副作用、且返回值就是布尔语义。

切换之后，图标的实际更新发生在 `ibus_rime_update_status` 里——这正是 4.1 流程图里 `update` 的下游：

[rime_engine.c:L250-L281](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L250-L281) —— 取出列表第 0 个属性（即 InputMode），按 `is_disabled` / `is_ascii_mode` 三种情况切换图标与文字，并同步 symbol。

```c
IBusProperty* prop = ibus_prop_list_get(rime_engine->props, 0);
if (prop) {
  if (!status || status->is_disabled) {
    icon = IBUS_RIME_ICONS_DIR "/disabled.png";
    label = ibus_text_new_from_static_string("維護");
  }
  else if (status->is_ascii_mode) {
    icon = IBUS_RIME_ICONS_DIR "/abc.png";
    label = ibus_text_new_from_static_string("Abc");
  }
  else {
    icon = IBUS_RIME_ICONS_DIR "/zh.png";
    /* schema_name is ".default" in switcher */
    if (status->schema_name && status->schema_name[0] != '.') {
      label = ibus_text_new_from_string(status->schema_name);
    }
    else {
      label = ibus_text_new_from_static_string("中文");
    }
  }
  // ... 设置 symbol、icon、label 后调 ibus_engine_update_property
}
```

这张三分支表值得记住：

| 条件 | 图标 | 文字 | 语义 |
| --- | --- | --- | --- |
| `is_disabled`（或 status 为空） | `disabled.png` | 「維護」 | librime 正在维护/部署，暂不可用 |
| `is_ascii_mode`（英文模式） | `abc.png` | 「Abc」 | ASCII 直出 |
| 其它（中文模式） | `zh.png` | schema 名或「中文」 | 正常中文输入 |

还有一处去重细节：函数开头有一段比较，若新旧 `is_disabled` / `is_ascii_mode` / `schema_id` 全都没变就直接 `return`（[rime_engine.c:L233-L240](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L233-L240)），避免无谓的属性更新抖动——但点击 InputMode 必然改变 `is_ascii_mode`，所以这条路径上不会命中去重。

另一个值得注意的点是 `ibus_prop_list_get(rime_engine->props, 0)` 用了**硬编码索引 0**。这依赖于 4.2.3 里 InputMode 是第一个被 `append` 的按钮。如果把按钮创建顺序调换，这里就要跟着改——这是一个隐藏的耦合点。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「点击中英文切换按钮」的完整数据流，看清图标是怎么联动改变的。

**操作步骤**：

1. 假设当前处于中文模式（`ascii_mode == False`），用户点击 InputMode 按钮。
2. 顺着下面这条链读代码，标注每一步的状态：
   - 入口：[rime_engine.c:L569](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L569) `get_option` 读到 `False`，`set_option` 写入 `True`。
   - 进入 `ibus_rime_engine_update`（[L283](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L283)），它调用 `get_status` 拿到 `status.is_ascii_mode == True`。
   - 进入 `ibus_rime_update_status`（[L230](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L230)），命中 `else if (status->is_ascii_mode)` 分支。
   - 图标被设为 `abc.png`，文字被设为「Abc」（[L258-L261](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L258-L261)）。
   - `ibus_engine_update_property` 把新属性推回 panel（[L279](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L279)）。

**需要观察的现象**：从「写一个选项」到「屏幕图标刷新」，中间经过 `update → get_status → update_status` 三跳。

**预期结果**：你能画出这条链路，并指出图标切换的真正决策点是在 `update_status` 的三分支判断里，而不是在 `property_activate` 本身——后者只负责改选项值。

> 本实践为源码阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`ascii_mode` 与 u3-l2 讲过的 `_horizontal` 都是 librime 会话选项，它们在命名上有何区别？这个区别暗示了什么？

**参考答案**：`ascii_mode` 没有下划线前缀，是 librime **标准用户选项**，会被方案配置识别、可在用户词库里持久化、许多方案会响应它；`_horizontal` 带下划线前缀，是 ibus-rime **前端专用**的内部选项，仅用于把候选表方向告知方案（如方案切换器），不被 librime 当作普通用户选项对待。前缀是「内部/外部」的命名约定。

**练习 2**：如果把 InputMode 按钮从 `props` 列表的第 0 个位置挪到第 2 个，`property_activate` 里的 InputMode 分支还能正常触发吗？图标还会正确更新吗？

**参考答案**：`property_activate` 仍能触发——它靠 `prop_name == "InputMode"` 字符串匹配，与位置无关。但图标**不会**正确更新，因为 `update_status` 用硬编码 `ibus_prop_list_get(props, 0)` 取第 0 个属性来改图标，挪到第 2 个后取到的就是别的按钮了。这正是文中提到的「隐藏耦合点」。

---

### 4.4 Deploy / Sync：重启 librime 与同步用户数据

#### 4.4.1 概念说明

「部署」和「同步」是两个面向用户词库与方案的高阶操作：

- **部署（Deploy）**：让 librime 重新读取方案配置、重新编译二进制词典、重建索引。在 ibus-rime 里，它的实现是**把整个 librime 核心关掉再重启**，因为 librime 的部署流程涉及大量状态重置，最干净的做法就是 finalize 后重新 initialize。
- **同步（Sync）**：把当前会话里学到的用户词频、用户词等数据写回磁盘（用户数据目录 `~/.config/ibus/rime`），或与同步盘交换数据。librime 提供了 `sync_user_data()` 接口来完成。

这两个操作都涉及入口层的生命周期函数，因此需要先理解 `ibus_rime_start` / `ibus_rime_stop` 这对封装（u2-l3 已铺垫过它们的语义）。

#### 4.4.2 核心流程

**Deploy 分支**：

```text
点击 deploy 按钮
        │
        ▼
ibus_rime_stop()            // = rime_api->finalize()  关闭核心
        │                     旧会话句柄随之失效！
        ▼
ibus_rime_start(TRUE)       // 重新 initialize
        │                     full_check=TRUE → start_maintenance 必然执行
        │                     → deploy_config_file("ibus_rime.yaml", ...)
        ▼
ibus_rime_engine_update()   // 拉取新状态刷新 UI
```

这里有一个**关键的副作用**：`finalize` 会让此前所有 `RimeSessionId` 失效。所以部署完成后，引擎里原有的 `session_id` 已是「野指针式」的过期句柄。所幸 u3-l2 学过的 `process_key_event` 在每次按键前都会 `find_session` 防御，下一次按键时会自动重建会话——这是 ibus-rime 能扛住「在线部署」的设计关键。

**Sync 分支**更微妙，源码注释专门解释了一种竞态：

```text
点击 sync 按钮
        │
        ▼
rime_api->sync_user_data()
        │
        ▼
ibus_rime_engine_update()
```

注释（[rime_engine.c:L559-L565](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L559-L565)）说明了 `sync_user_data` 的两种处境：

1. 若启动时 `start_maintenance` 已经起了一个后台维护线程在跑，那么 `sync_user_data` **不会立即同步**，而是把同步任务**排进那个线程的队列**，然后立即返回 `False`。
2. 但存在竞态：维护线程可能**正好在退出**，新排进去的任务就被遗漏、没人执行了。

换句话说，「同步」按钮在维护线程活跃时是「预约」，而不是「立即执行」。

#### 4.4.3 源码精读

先看 deploy 分支，注意 `ibus_rime_start(TRUE)` 里那个 `TRUE`：

[rime_engine.c:L553-L557](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L553-L557) —— deploy 按钮通过「先 stop 后 start(TRUE)」重启 librime，`TRUE` 强制做一次完整维护检查。

```c
  if (!strcmp("deploy", prop_name)) {
    ibus_rime_stop();
    ibus_rime_start(TRUE);
    ibus_rime_engine_update(rime_engine);
  }
```

要理解 `TRUE` 的作用，必须回到入口层看 `ibus_rime_start` 的实现：

[rime_main.c:L66-L81](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L66-L81) —— 建用户目录、填 traits、`initialize`，并在 `start_maintenance(full_check)` 返回真（即确实启动了维护）时，额外 `deploy_config_file` 刷新前端配置。

```c
void ibus_rime_start(gboolean full_check) {
  char user_data_dir[512] = {0};
  get_ibus_rime_user_data_dir(user_data_dir);
  if (!g_file_test(user_data_dir, G_FILE_TEST_IS_DIR)) {
    g_mkdir_with_parents(user_data_dir, 0700);
  }
  RIME_STRUCT(RimeTraits, ibus_rime_traits);
  fill_traits(&ibus_rime_traits);
  ibus_rime_traits.user_data_dir = user_data_dir;

  rime_api->initialize(&ibus_rime_traits);
  if (rime_api->start_maintenance((Bool)full_check)) {
    // update frontend config
    rime_api->deploy_config_file("ibus_rime.yaml", "config_version");
  }
}
```

`full_check` 的含义在这里浮现：它是 `start_maintenance` 的参数。启动时（`rime_with_ibus` 里调用 `ibus_rime_start(FALSE)`，见 [rime_main.c:L125-L126](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L125-L126)）用 `FALSE` 做**轻量**检查，避免每次开机都全量部署拖慢启动；而部署按钮用 `TRUE` 做**完整**检查，强制触发维护与配置刷新。这是同一函数在两种场景下的参数取舍。

再看 `ibus_rime_stop`，它非常薄：

[rime_main.c:L83-L87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L83-L87) —— 带防御地调用 `finalize`，关闭 librime 核心。

```c
void ibus_rime_stop() {
  if (rime_api) {
    rime_api->finalize();
  }
}
```

`if (rime_api)` 是必要的防御：`rime_api` 是全局指针，极端情况下（例如部署按钮在启动早期被点击）可能尚未赋值。注意 `finalize` 之后**没有**把 `rime_api` 置空——它只是销毁了 librime 的内部状态，API 函数指针表本身仍然有效，所以紧接着的 `ibus_rime_start(TRUE)` 才能再次调用 `rime_api->initialize`。这是「RimeApi 作为稳定边界」（u6-l2 会专题讲）的一个具体体现：句柄不变，状态可重生。

最后细读 sync 分支的注释，这是本讲最值得咀嚼的一段：

[rime_engine.c:L558-L568](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L558-L568) —— sync 按钮直接调 `sync_user_data`，注释详述了它与后台维护线程的交互及潜在竞态。

```c
  else if (!strcmp("sync", prop_name)) {
    // in the case a maintenance thread has already been started
    // by start_maintenance(); the following call to sync_user_data()
    // will queue data synching tasks for execution in the working
    // maintenance thread, and will return False.
    // however, there is still chance that the working maintenance thread
    // happens to be quitting when new tasks are added, thus leaving newly
    // added tasks undone...
    rime_api->sync_user_data();
    ibus_rime_engine_update(rime_engine);
  }
```

这段注释透露的 librime 行为模型：

- **维护线程是异步的**：`start_maintenance` 启动一个后台线程做编译词典、部署方案等耗时活儿，主线程不阻塞。
- **`sync_user_data` 的语义依赖上下文**：维护线程活跃时它是「排队」，否则可能立即执行。
- **存在已知的竞态漏洞**：维护线程退出与任务入队的时序错配，可能导致同步任务丢失。注释坦诚地留下了这个限制，没有用锁去强行修补——这是上游 librime 行为决定的，前端只能如实转发。

#### 4.4.4 代码实践

**实践目标**：解释「部署」按钮 `ibus_rime_stop + ibus_rime_start(TRUE)` 的完整作用，并说明「同步」按钮与维护线程的关系。这是本讲规格指定的核心实践。

**操作步骤**：

1. 打开 [rime_engine.c:L553-L557](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L553-L557)，确认 deploy 分支调用了 `ibus_rime_stop()` 和 `ibus_rime_start(TRUE)`。
2. 跳到 [rime_main.c:L66-L87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L66-L87)，把这两个函数展开（代入实现），写出 deploy 等价于下面这串 librime 调用：
   - `rime_api->finalize()`
   - （重建用户目录、填 traits）
   - `rime_api->initialize(&traits)`
   - `rime_api->start_maintenance(TRUE)` → 若返回真 → `rime_api->deploy_config_file("ibus_rime.yaml", "config_version")`
3. 思考：部署之后，引擎里原来的 `session_id` 还有效吗？下一次按键会发生什么？结合 u3-l2 的 `find_session` 防御回答。
4. 阅读 [rime_engine.c:L558-L568](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L558-L568) 的注释，用自己的话复述「维护线程活跃时点同步」与「维护线程正在退出时点同步」两种情形下 `sync_user_data` 的行为差异。

**需要观察的现象**：

- deploy 把 `finalize` 与 `initialize` 配对使用，构成「软重启」。
- `TRUE` 与启动时的 `FALSE` 形成对照，体现了「按钮触发=强检查」「开机=轻检查」的策略。
- sync 的返回值被代码**忽略**（没有判断 `sync_user_data()` 的真假），意味着前端不区分「立即完成」与「排队等候」。

**预期结果**：

- **deploy 的作用**：等价于「关掉 librime 核心 → 重新初始化 → 强制做一次完整维护并刷新 `ibus_rime.yaml`」。它让用户改过的方案配置、新装的数据包真正生效。副作用是旧会话全部失效，依赖 `process_key_event` 里的 `find_session` 在下次按键时重建会话。
- **sync 与维护线程的关系**：若维护线程已在跑，`sync_user_data` 把同步任务排队进该线程并立即返回 `False`（预约而非立即）；但若维护线程恰好正在退出，新任务可能被遗漏——这是源码注释明确指出的已知限制。前端不锁、不重试，只如实转发调用。

> 若要在真实环境验证维护线程的行为，需在 Linux + IBus + librime 的桌面环境里操作，本环境无法运行图形界面，故上述行为细节标注为「依据源码与注释推导」，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 deploy 用 `ibus_rime_start(TRUE)`，而进程启动时（`rime_with_ibus`）用 `ibus_rime_start(FALSE)`？

**参考答案**：`TRUE`/`FALSE` 是 `start_maintenance` 的 `full_check` 参数。启动时用 `FALSE` 做轻量检查，避免每次开机都全量编译词典、拖慢登录后立即可用性；用户主动点「部署」则意味着「我知道我改了配置、请彻底重建」，用 `TRUE` 强制完整检查，确保改动一定生效。同一函数、不同参数服务于两种语义。

**练习 2**：部署完成后，引擎成员 `session_id` 里的旧值还能直接拿去 `process_key` 吗？为什么 ibus-rime 不会因此崩溃？

**参考答案**：不能。`finalize` 已让旧 `session_id` 失效。但 `process_key_event`（u3-l2）在按键前先调 `rime_api->find_session(rime_engine->session_id)`，发现找不回就调 `ibus_rime_create_session` 重建一个新会话再继续。正是这道防御让「在线部署」不会导致后续按键崩溃。

**练习 3**：`sync_user_data()` 的返回值在代码里被忽略了。如果让你改进，你会如何利用这个返回值给用户更好的反馈？

**参考答案**：可以区分两种情况给不同的桌面通知——返回 `True` 表示已立即完成，提示「同步完成」；返回 `False` 表示已排队（或可能丢失），提示「同步已排队，将在维护完成后执行」。可以复用 `rime_main.c` 里的 `show_message`（[rime_main.c:L32-L36](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L32-L36)）来弹通知。不过要注意，注释指出的竞态（线程正好退出）即便有返回值也无法完全覆盖，仍需上游 librime 配合。

## 5. 综合实践

**综合任务**：绘制一张「状态栏按钮 → librime 操作 → UI 反馈」的完整时序图，把本讲四个模块串起来。

请按下列步骤完成：

1. **画按钮列**：在纸或绘图工具上画出三个状态栏按钮，标注它们的 `key`（`InputMode` / `deploy` / `sync`）、图标（`zh.png` / `reload.png` / `sync.png`）、在 `props` 列表中的索引（0 / 1 / 2）。依据 [rime_engine.c:L114-L152](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L114-L152)。

2. **画分发器**：从每个按钮画一条箭头指向 [rime_engine.c:L546-L575](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L546-L575) 里对应的 `strcmp` 分支，标注匹配的字符串。

3. **画 librime 操作**：从每个分支画出它实际调的 librime 接口：
   - InputMode → `get_option`/`set_option("ascii_mode", ...)`
   - deploy → `finalize`（经 `ibus_rime_stop`）+ `initialize`/`start_maintenance(TRUE)`/`deploy_config_file`（经 `ibus_rime_start`）
   - sync → `sync_user_data`（标注「排队或立即，依维护线程而定」）

4. **画 UI 反馈回路**：每条路径都回到 `ibus_rime_engine_update`，再分流到：
   - `get_status` → `update_status` → 改 InputMode 图标（仅第 0 个属性）
   - `get_commit` / `get_context` → 预编辑、候选表（u4 专题）

5. **标注副作用**：在 deploy 那条路径上加一个红色标记「旧 session_id 失效，依赖下次按键的 find_session 重建」。

**预期产出**：一张能让人「按图索骥」读懂状态栏全流程的时序图。画完后，你应能用一句话回答：**「ibus-rime 的状态栏按钮从不直接操作 UI，它们只改 librime 的选项/状态，UI 更新统一由 `ibus_rime_engine_update` 在事后拉取刷新。」** 这正是薄前端「转发 + 投影」架构的精髓。

## 6. 本讲小结

- ibus-rime 只在 **`focus_in`、`reset`、`disable`** 三个生命周期回调里写实质逻辑，`focus_out`/`enable` 刻意留空——薄前端不重复 librime 已能管理的状态。
- `focus_in` 做三件事：重新 `register_properties`（因为 panel 不保留状态）、找回或重建会话、刷新一次 UI；它与 `disable`（销毁会话）构成对称的「清零-补建」关系。
- `property_activate` 是一个字符串分发器，用 `strcmp` 把 `prop_name` 路由到 deploy / sync / InputMode 三条路径，**字符串必须与按钮创建时的 `key` 逐字符一致**（大小写敏感）。
- **InputMode** 通过翻转 `ascii_mode` 会话选项切换中英文，真正的图标变化发生在 `update_status` 的三分支判断里（`disabled` / `abc` / `zh`）。
- **Deploy** 等价于 `ibus_rime_stop + ibus_rime_start(TRUE)`，即 finalize 后重新 initialize 并强制完整维护；副作用是旧会话失效，依赖 `process_key_event` 的 `find_session` 兜底。
- **Sync** 调 `sync_user_data`，其语义依赖后台维护线程：活跃时排队、退出竞态时可能丢失——源码注释坦诚记录了这一上游限制。

## 7. 下一步学习建议

本讲把「状态栏按钮 → librime 操作」这条链讲透了，但「librime 操作 → 屏幕 UI」的后半段——`ibus_rime_engine_update` 内部如何拉取 status/commit/context 并渲染——只是点到为止。下一步建议进入 **U4 前端 UI 渲染**：

- **u4-l1 综合更新函数与状态同步**：精读 `ibus_rime_engine_update` 的三段式（status/commit/context），以及 `update_status` 的去重逻辑，把本讲里反复出现的「`update` 刷新 UI」展开到底。
- **u4-l2 预编辑与辅助文本渲染**：搞清 `preedit_style`、`cursor_type` 如何影响内联预编辑。
- **u4-l3 候选词列表渲染**：`RimeContext.menu` 如何翻译成 `IBusLookupTable`。

读完 U4，再回头看本讲的 deploy/sync 路径，你会对「按钮点击后屏幕为什么这么变」有完整的端到端理解。随后 **u5-l1** 会讲 `ibus_rime.yaml` 如何驱动这些 UI 行为，本讲里出现的 `g_ibus_rime_settings.lookup_table_orientation` 等设置项的来源也会水落石出。
