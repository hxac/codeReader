# 会话管理与按键处理主链路

## 1. 本讲目标

上一讲（u3-l1）我们搭好了 `IBusRimeEngine` 的「类型骨架」，知道了一个引擎实例有 `session_id`、`status`、`table`、`props` 四个成员，也看到 `init` 里第一件事就是建会话：

[文件 rime_engine.c:100-103](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L100-L103) —— `init` 一进来就调 `ibus_rime_create_session`。

但「会话是什么、它什么时候被建、什么时候会失效、失效了怎么办」——u3-l1 故意没展开，全部留到本讲。本讲的另一条主线是 `class_init` 里挂载的第一个、也是最关键的虚函数 `process_key_event`：

[文件 rime_engine.c:77](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L77-L77) —— `engine_class->process_key_event = ibus_rime_engine_process_key_event;`

它是「按键从 IBus 框架进入 librime 核心引擎」的唯一入口，也是整个 ibus-rime「薄前端」定位最集中的体现：**前端只做过滤、转发、同步状态，真正的按键处理交给 librime。**

学完本讲你应当能够：

- 说清楚 `RimeSessionId` 的完整生命周期——在 `init` / `focus_in` / `process_key_event` / `disable` / `destroy` 这五处分别发生了什么，以及为什么 `process_key_event` 里要做 `find_session` 防御；
- 画出 `ibus_rime_engine_process_key_event` 的处理流水线，标注一个按键从 IBus 到 librime 的完整路径；
- 解释为什么要过滤 `IBUS_SUPER_MASK | IBUS_MOD4_MASK`，以及 `return FALSE` 对调用方意味着什么；
- 解释为什么 `_horizontal` 选项必须在每次 `process_key` 之前同步（而不是建会话时设置一次），这对应 1.6.1 的「arrow key orientation」修复；
- 区分 `set_option` / `get_option` 的用法，看懂 `soft_cursor`、`_horizontal`、`ascii_mode` 三个运行时开关的设计。

## 2. 前置知识

本讲要用到几个概念，先用通俗的话过一遍。

- **会话（session）。** 在 librime 里，一次「输入过程」的全部状态——当前输入的拼音串、候选词列表、当前所在的方案（schema）、中/英文模式——都装在一个叫「会话」的对象里。每个会话有一个不透明的整数句柄 `RimeSessionId`。**不同会话之间状态完全隔离**，这正是「同时开两个输入框、互不干扰」的实现基础。librime 用 `create_session` 建会话、`destroy_session` 销毁、`find_session` 检查某个 id 是否还活着。
- **`rime_api` 全局指针。** u2-l1 讲过，`main()` 里用 `rime_get_api()` 拿到指向 librime 的 `RimeApi` 句柄，存进全局 `rime_api`。本讲里所有「建会话、查会话、处理按键、读写选项」都是经这个指针调进 librime 的。
- **IBus 引擎虚函数 `process_key_event`。** u3-l1 讲过 `class_init` 里挂虚函数表。`IBusEngine` 基类规定：每当用户敲一个键，IBus 守护进程就会调用当前引擎的 `process_key_event(engine, keyval, keycode, modifiers)`。它的返回值是 `gboolean`：**返回 `TRUE` 表示「这个键我（输入法）处理了」，IBus 就不会再把它发给应用；返回 `FALSE` 表示「我不管」，按键继续传给应用。** 这条返回值语义是本讲修饰键过滤的关键。
- **修饰键掩码（modifier mask）。** 一个按键事件除了「是哪个键（`keyval`）」，还带一组修饰键状态，用位掩码表示：`IBUS_SHIFT_MASK`（Shift）、`IBUS_CONTROL_MASK`（Ctrl）、`IBUS_MOD1_MASK`（通常是 Alt）、`IBUS_SUPER_MASK` / `IBUS_MOD4_MASK`（通常是 Super/Win 键）、`IBUS_LOCK_MASK`（Caps Lock）、`IBUS_RELEASE_MASK`（是「松开」事件而非「按下」）。用 `modifiers & 某个_MASK` 就能判断对应修饰键是否按下。
- **`g_ibus_rime_settings` 全局设置。** u5-l1 会详讲，本讲只需知道它是 `ibus_rime.yaml` 加载后的产物，其中 `lookup_table_orientation` 决定候选词表横排还是竖排。

如果你对 `rime_api`、`IBusRimeEngine` 类型与 `init`/`destroy` 还不熟，建议先看 u2-l1 与 u3-l1。

## 3. 本讲源码地图

本讲几乎全部集中在 `rime_engine.c` 一个文件里，而且核心就是两个函数：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) | 引擎层全部实现 | `ibus_rime_create_session`、`process_key_event`、以及会话在三处的销毁 |

辅助理解还会顺带提到设置层与配置：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [rime_settings.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c) | 加载 `ibus_rime.yaml` | `lookup_table_orientation` 的默认值与来源 |
| [rime_settings.h](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h) | 设置结构体声明 | `IBusRimeSettings` 的成员 |
| [ibus_rime.yaml](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml) | 运行时配置 | `style/horizontal` 选项 |

本讲**不**展开 `process_key_event` 末尾调用的 `ibus_rime_engine_update`（它负责把 librime 的状态渲染成预编辑文本、候选表、状态栏）——那是 u4 的主题。本讲只回答：**一个按键进来之后，在被 librime 处理之前，前端都做了哪些「门卫」工作；以及承载这次输入的会话，是怎么被找到、必要时重建出来的。**

## 4. 核心概念与源码讲解

按规格，本讲拆成五个最小模块，正好对应一条完整的按键主链路：

1. **Rime 会话的 create / find / destroy**——会话句柄的全生命周期；
2. **`process_key_event` 按键处理主链路**——把整个流水线先看全；
3. **修饰键过滤**——为什么先放行 Super/Mod4，再裁剪掩码；
4. **`_horizontal` 方向同步**——为什么要在每次按键前同步（含最近的 arrow key orientation 修复）；
5. **`set_option` / `get_option`**——前端与核心之间的运行时开关。

### 4.1 Rime 会话：create / find / destroy

#### 4.1.1 概念说明

librime 是个「无状态的服务」吗？不是。它的核心状态被切分成一个个**会话（session）**。一次会话持有「用户正在输入什么、当前选中哪个方案、候选词翻到第几页」等全部上下文。前端（ibus-rime）拿到的是一个不透明整数句柄 `RimeSessionId`，之后所有「处理按键、取状态、取候选」都要先报上这个句柄。

为什么需要会话机制？因为同一时刻可能有多个输入焦点（多个输入框、多个窗口），它们的输入状态互不相同——A 框在打中文、B 框要纯英文。给每个 `IBusRimeEngine` 实例配一个会话，状态天然隔离。

会话的生命周期由 librime 的三个 API 控制：

- `create_session()` —— 建一个新会话，返回新句柄；
- `find_session(id)` —— 查句柄 `id` 是否还活着，返回 `True/False`；
- `destroy_session(id)` —— 销毁句柄 `id` 对应的会话。

#### 4.1.2 核心流程

一个 `session_id` 在引擎层经历的状态可以用下面这张状态机描述：

```
                 create_session()
   (无会话) ───────────────────────► (会话活跃, session_id != 0)
        ▲                                  │
        │                                  │ find_session(id) == False
        │ create_session()                 │ (核心重启/部署导致旧会话失效)
        │                                  ▼
        │                                  (重建会话)
        │ destroy_session()                │
        └──────────────────────────────────┘
                  destroy_session()
```

关键洞察：**「会话失效」是会发生的，而且不是 bug。** 当用户点击状态栏的「部署」按钮（u3-l3 会详讲），前端会调用 `ibus_rime_stop()` + `ibus_rime_start(TRUE)`——也就是先 `finalize` 整个 librime、再重新 `initialize`。这一次重启会让 librime 内部**所有已建会话全部作废**。而 `IBusRimeEngine` 实例本身并不销毁（它属于 IBus，不在 librime 的管辖范围），它的 `session_id` 成员里仍然存着那个已经死掉的旧句柄。

所以前端不能假设「我 `init` 时建的会话永远有效」——每次要用会话之前，必须先用 `find_session` 探一下；探不到就重新 `create_session`。这就是 `process_key_event` 与 `focus_in` 里那段防御性代码的来历。

#### 4.1.3 源码精读

先看建会话的小助手，它被 `init`、`focus_in`、`process_key_event` 三处共用：

[文件 rime_engine.c:89-98](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L89-L98) —— 建会话并设置首个运行时选项 `soft_cursor`：

```c
rime_engine->session_id = rime_api->create_session();
Bool inline_caret =
    g_ibus_rime_settings.embed_preedit_text &&
    g_ibus_rime_settings.preedit_style == PREEDIT_STYLE_COMPOSITION &&
    g_ibus_rime_settings.cursor_type == CURSOR_TYPE_INSERT;
rime_api->set_option(rime_engine->session_id, "soft_cursor", !inline_caret);
```

注意三件事：

1. 它把 librime 返回的新句柄直接写进 `rime_engine->session_id`——**覆盖式赋值**，所以调用方必须保证此时旧会话已经处理干净，否则会泄漏旧句柄。
2. 建完立刻 `set_option("soft_cursor", ...)`，这属于 4.5 节要讲的「会话级运行时选项」；现在只需知道「建会话时要顺手把样式相关的选项灌进去」。
3. 它依赖全局 `g_ibus_rime_settings`（u5-l1 的配置产物），所以会话其实是「按当前配置」建的。

再看会话**销毁**的两处。一处是引擎析构时：

[文件 rime_engine.c:158-161](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L158-L161) —— `destroy` 里销毁会话并清零句柄：

```c
if (rime_engine->session_id) {
  rime_api->destroy_session(rime_engine->session_id);
  rime_engine->session_id = 0;
}
```

另一处是引擎被禁用时：

[文件 rime_engine.c:224-227](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L224-L227) —— `disable` 回调里同样销毁会话并清零：

```c
if (rime_engine->session_id) {
  rime_api->destroy_session(rime_engine->session_id);
  rime_engine->session_id = 0;
}
```

两处代码完全一样，都要做两件事：`destroy_session` 让 librime 回收会话内存，再把本地句柄**置 0**。置 0 是关键——它让后续的判空（`if (session_id)`）能识别「当前没有会话」，避免拿着野句柄继续调 librime。这也解释了 u3-l1 里 `destroy` 为何每个资源释放都判空：因为 `disable` 可能已经先把会话销毁过一次了。

最后看会话**查找/重建**。在 `focus_in` 里（输入框获得焦点时）：

[文件 rime_engine.c:190-192](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L190-L192) —— 拿到焦点时若没有会话就补建一个：

```c
if (!rime_engine->session_id) {
  ibus_rime_create_session(rime_engine);
}
```

注意 `focus_in` 这里用的是「`session_id` 是否为 0」做判断，而不是 `find_session`——因为焦点切换时引擎实例刚被 IBus 拿出来用，会话要么是 `init` 时建好的、要么是 `disable` 清掉的，本地状态是可信的。而在 `process_key_event` 里，前端面对的是「librime 可能已被重启过」的不可信场景，所以改用更严格的 `find_session`，这一点 4.2 节会看到。

#### 4.1.4 代码实践

**实践目标：** 跟踪一次「点击部署按钮 → 再敲一个键」的过程中，`session_id` 经历了什么，验证 `find_session` 的防御作用。

**操作步骤：**

1. 阅读 [rime_engine.c:553-557](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L553-L557) 的 `deploy` 分支，确认它调用 `ibus_rime_stop()` + `ibus_rime_start(TRUE)`，即重启了整个 librime。
2. 想象接下来用户敲一个字母键，IBus 调用 `process_key_event`。对照 [rime_engine.c:525-527](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L525-L527)，回答：此时 `rime_engine->session_id` 还是不是 0？`find_session` 会返回什么？
3. 解释：如果 4.2 节那段 `find_session` 检查不存在，会发生什么？

**预期结果：** `session_id` 仍是部署前的旧值（非 0，因为 `deploy` 分支只重启了 librime，没碰引擎实例的成员）。但 librime 内部的会话表已经被 `finalize` 清空，所以 `find_session(旧id)` 返回 `False`，于是触发 `ibus_rime_create_session` 建一个全新会话、拿到新句柄。**如果没有这段 `find_session` 检查，前端就会拿着旧句柄去调 `process_key`，librime 找不到会话，按键直接丢失**——用户部署后会发现「输入法没反应了」。

**待本地验证：** 若想亲眼看这条路径，可在 `ibus_rime_create_session` 入口加一行 `g_debug("create_session: old=%lu", (gulong)rime_engine->session_id);`、在 `process_key_event` 的 `find_session` 分支加一行 `g_debug("find_session miss, recreating");`，重新编译后重启 IBus，点一次「部署」再敲键，观察日志是否出现「find_session miss」。

#### 4.1.5 小练习与答案

**练习 1：** `destroy`（析构）和 `disable`（禁用）里都写了同样的销毁会话代码，为什么不抽成一个共用函数？这样重复有什么风险？

**参考答案：** 两段逻辑确实一样，理论上可以抽成一个 `ibus_rime_destroy_session(rime_engine)` 小函数来去重。本项目选择直接重复，可能是因为逻辑太短（3 行）且语义清晰。风险是「以后改销毁逻辑（比如加一个清理步骤）时可能漏改其中一处」，导致两处行为不一致。这是工程上「重复 vs 抽象」的常见取舍。

**练习 2：** 为什么 `focus_in` 用 `if (!session_id)` 判断，而 `process_key_event` 用 `if (!find_session(session_id))` 判断？两者分别在防什么？

**参考答案：** `focus_in` 防的是「本地句柄为空」（典型场景：`disable` 刚把会话清成 0），这时本地状态可信，直接看是否为 0 即可。`process_key_event` 防的是更隐蔽的「本地句柄非空、但 librime 那边已经不认了」（典型场景：部署重启了 librime），本地无法察觉这种失效，必须主动 `find_session` 去问 librime。两者覆盖的失效场景不同，所以判断方式也不同。

### 4.2 process_key_event：按键处理主链路

#### 4.2.1 概念说明

`process_key_event` 是 `IBusEngineClass` 最重要的虚函数。每当用户敲键（按下或松开），IBus 守护进程就会调用当前引擎的这个函数，把按键信息打包传进来。ibus-rime 作为「薄前端」，它的职责不是自己解析按键，而是：

1. 决定这个键要不要转给 librime（过滤）；
2. 保证有一个可用的会话来承接这个键；
3. 把按键信息（键值 + 修饰键）同步给 librime，让核心引擎去查词、更新候选；
4. 把 librime 处理后的新状态刷到 UI（这一步实际委托给 `ibus_rime_engine_update`，属于 u4）。

整个过程可以浓缩成一句话：**「门卫检查 → 找会话 → 同步方向 → 转交按键 → 刷新界面」。**

#### 4.2.2 核心流程

`ibus_rime_engine_process_key_event` 的完整流水线（按代码顺序）：

1. **过滤 Super/Mod4 修饰键**：带 Super 的键直接 `return FALSE`，放行给系统/应用。
2. **把 `engine` 指针 cast 成子类视角** `IBusRimeEngine *`，以便访问 `session_id` 等成员。
3. **裁剪修饰键掩码**：只保留 librime 关心的那几位修饰键。
4. **保证会话可用**：`find_session` 探测，探不到就 `create_session`；若仍没有会话（核心服务被禁用），直接 `update` 后 `return FALSE`。
5. **同步 `_horizontal` 方向选项**：按当前配置刷新候选表方向（4.4 节详讲）。
6. **转交按键**：`rime_api->process_key(session_id, keyval, modifiers)`，由 librime 真正处理。
7. **刷新 UI**：`ibus_rime_engine_update`（u4 详讲）。
8. **返回 librime 的处理结果**：librime 说「处理了」就返回 `TRUE`，否则 `FALSE`。

这条流水线里，第 1、3 步是「修饰键过滤」（4.3 节），第 5 步是「方向同步」（4.4 节），第 4 步依赖 4.1 节的会话机制。

#### 4.2.3 源码精读

整个函数只有 30 多行，但每一段都是一个「职责段」。先看函数签名与第一步过滤：

[文件 rime_engine.c:509-518](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L509-L518) —— 函数签名与 Super/Mod4 过滤（4.3 节展开）：

```c
static gboolean
ibus_rime_engine_process_key_event (IBusEngine *engine,
                                    guint       keyval,
                                    guint       keycode,
                                    guint       modifiers)
{
  // ignore super key, @see ibus_engine_filter_key_event
  if (modifiers & (IBUS_SUPER_MASK | IBUS_MOD4_MASK)) {
    return FALSE;
  }
```

接着是 cast、掩码裁剪、会话保证三段：

[文件 rime_engine.c:520-531](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L520-L531) —— 裁剪修饰键、找回会话、处理「服务被禁用」：

```c
  IBusRimeEngine *rime_engine = (IBusRimeEngine *)engine;

  modifiers &= (IBUS_RELEASE_MASK | IBUS_LOCK_MASK | IBUS_SHIFT_MASK |
                IBUS_CONTROL_MASK | IBUS_MOD1_MASK);

  if (!rime_api->find_session(rime_engine->session_id)) {
    ibus_rime_create_session(rime_engine);
  }
  if (!rime_engine->session_id) {  // service disabled
    ibus_rime_engine_update(rime_engine);
    return FALSE;
  }
```

注意第二个 `if` 的注释 `// service disabled`：即便刚 `create_session` 过，如果 librime 处于「被禁用」状态（比如正在维护/部署中），`create_session` 会返回 0（无效句柄），`session_id` 仍为 0。这时前端不处理按键，但仍然调一次 `update`——为了让状态栏正确显示「維護（维护中）」图标（u4-l1 会看到 `update_status` 里 `is_disabled` 分支）。

然后是方向同步与转交：

[文件 rime_engine.c:533-544](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L533-L544) —— 同步 `_horizontal`、转交按键、刷新、返回（4.4 节展开前两段）：

```c
  // Arrow key actions respect candidate list orientation.
  Bool horizontal = g_ibus_rime_settings.lookup_table_orientation ==
                    IBUS_ORIENTATION_HORIZONTAL;
  if (rime_api->get_option(rime_engine->session_id, "_horizontal") != horizontal) {
    rime_api->set_option(rime_engine->session_id, "_horizontal", horizontal);
  }

  gboolean result =
      rime_api->process_key(rime_engine->session_id, keyval, modifiers);
  ibus_rime_engine_update(rime_engine);
  return result;
}
```

读到这里，整条主链路就清楚了：**`process_key_event` 本身几乎不含「业务逻辑」，它是一道流水线，把按键干净地送到 librime，再把 librime 的状态干净地送到 UI。** 这正是「薄前端」的精髓——前端不查词、不分词，只做编排。

#### 4.2.4 代码实践

**实践目标：** 在 `process_key_event` 中标注「一个按键从 IBus 进入到 librime」的完整路径，并解释每一道关卡的作用。

**操作步骤：**

1. 打开 [rime_engine.c:509-544](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L509-L544)，按 4.2.2 的八步流水线，在每一步旁标注「它做了什么、如果省略会怎样」。
2. 特别追踪这三个参数 `keyval`、`keycode`、`modifiers`：哪个被过滤？哪个被裁剪？哪个最终原样传给 `rime_api->process_key`？
3. 回答：`keycode`（物理键码）在整个函数里被用到了吗？

**预期结果（参数流）：**

| 参数 | 进入时 | 在函数内的命运 |
| --- | --- | --- |
| `keyval` | IBus 给的逻辑键值 | **原样**传给 `rime_api->process_key` |
| `keycode` | IBus 给的物理键码 | 全程**未被使用**（librime 只用 `keyval`） |
| `modifiers` | IBus 给的完整修饰掩码 | 先过滤（Super/Mod4 直接 return），再**裁剪**成 5 位，最后传给 `process_key` |

**关于关卡作用的总结：** 过滤 Super 是为了让系统快捷键（如呼出活动概览）不被输入法吞掉；裁剪掩码是为了不让 librime 看到它不认识的修饰位；`find_session` 是为了扛住「部署重启 librime」导致的会话失效；`_horizontal` 同步是为了让候选表方向与配置一致。每一道关卡都对应一个真实问题的解法，删掉任何一道都会引发可观察的 bug。

#### 4.2.5 小练习与答案

**练习 1：** `process_key_event` 的返回值直接就是 `rime_api->process_key(...)` 的返回值。这说明了前端在「按键归属」上的什么态度？

**参考答案：** 前端把「这个键算不算被输入法处理了」的最终裁决权完全交给 librime。librime 返回 `True`（比如这个字母键确实进入了拼音串），前端就返回 `TRUE` 告诉 IBus「键被我吃了」；librime 返回 `False`（比如当前是英文模式、这个键不该拦），前端就返回 `FALSE`，让按键透传给应用。前端自己只在「过滤掉 Super」这一处独立地返回 `FALSE`，其余全听核心的。

**练习 2：** 为什么「服务被禁用」分支（`if (!session_id)`）里还要调一次 `ibus_rime_engine_update`，而不是直接 `return FALSE`？

**参考答案：** 因为状态栏需要反映「输入法正在维护中」。`update` 会调用 `get_status`/`update_status`，在 `is_disabled` 为真时把状态栏图标切换成 `disabled.png`、标签显示「維護」（见 u4-l1）。如果直接 return，状态栏就停留在上一次的图标，用户看不出输入法已被禁用。这个细节体现了「按键处理」与「状态展示」是耦合在同一个流水线里的。

### 4.3 修饰键过滤

#### 4.3.1 概念说明

按键事件带一个修饰键掩码 `modifiers`，它是一个位图，每一位代表一个修饰键。但并非所有修饰键都该被输入法处理——有些是系统级快捷键的领地。

这里有两层不同的「过滤」，很容易混淆，必须分清：

- **整键放行（drop）**：如果按键带了 Super（Win 键），整个事件直接 `return FALSE`，输入法完全不碰它。这是「这个键不属于我」。
- **掩码裁剪（mask）**：对于剩下的键，把 `modifiers` 里 librime 不关心的那些位清零，再把裁剪后的掩码传给 librime。这是「这个键属于我，但修饰键信息我帮你精简一下」。

返回值 `FALSE` 的语义在 4.2.1 已讲过：「我（输入法）不处理，请把按键继续传给应用/窗口管理器」。所以对 Super 键返回 `FALSE`，等于让 Super 快捷键（如系统活动概览、窗口移动）正常生效，不被输入法拦截。

#### 4.3.2 核心流程

两层过滤的形式化描述。设 `modifiers` 是 IBus 传入的完整位掩码，`SUPER = IBUS_SUPER_MASK | IBUS_MOD4_MASK`，`KNOWN = IBUS_RELEASE_MASK | IBUS_LOCK_MASK | IBUS_SHIFT_MASK | IBUS_CONTROL_MASK | IBUS_MOD1_MASK`：

\[ \text{若 } \; \text{modifiers} \,\&\, \text{SUPER} \neq 0 \;\Rightarrow\; \text{return FALSE} \]

\[ \text{否则} \;\; \text{modifiers}' = \text{modifiers} \,\&\, \text{KNOWN} \]

也就是说，先做一次「或检测」决定放行，再做一次「与裁剪」把掩码收窄到已知的 5 位（松开、CapsLock、Shift、Ctrl、Alt），最后把 `modifiers'` 传给 librime。

#### 4.3.3 源码精读

第一层「整键放行」：

[文件 rime_engine.c:515-518](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L515-L518) —— Super/Mod4 整键放行：

```c
  // ignore super key, @see ibus_engine_filter_key_event
  if (modifiers & (IBUS_SUPER_MASK | IBUS_MOD4_MASK)) {
    return FALSE;
  }
```

这段代码有段历史。最初它只过滤 `IBUS_SUPER_MASK`：

```c
if (modifiers & IBUS_SUPER_MASK) { return FALSE; }
```

后来在 1.5.0（提交 `3274f1e`，issue #192「ignore super modifier in gtk4」）改成同时过滤 `IBUS_MOD4_MASK`。原因是：在 GTK4 环境下，IBus 把 Super 键事件用 `IBUS_MOD4_MASK` 标记（而不是 `IBUS_SUPER_MASK`），只过滤前者会漏网，导致 Super 快捷键被输入法误拦。所以现在两个位一起检测，覆盖 GTK3 与 GTK4 两种标记方式。

第二层「掩码裁剪」：

[文件 rime_engine.c:522-523](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L522-L523) —— 把修饰掩码收窄到已知的 5 位：

```c
  modifiers &= (IBUS_RELEASE_MASK | IBUS_LOCK_MASK | IBUS_SHIFT_MASK |
                IBUS_CONTROL_MASK | IBUS_MOD1_MASK);
```

注意这是**就地修改参数 `modifiers`**（`&=`），所以后面传给 `rime_api->process_key` 的就是裁剪后的版本。被清掉的位包括 `IBUS_MOD2_MASK`（Num Lock）、`IBUS_MOD3_MASK`、`IBUS_MOD5_MASK`（Scroll Lock）等——这些「亮着但不影响输入语义」的修饰位，librime 不需要看到，提前剥离能避免 librime 把它们误判成「用户按了某个组合键」。

#### 4.3.4 代码实践

**实践目标：** 区分「整键放行」与「掩码裁剪」，并预测几种按键事件的命运。

**操作步骤：**

1. 对照 [rime_engine.c:515-523](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L515-L523) 的两段过滤，填写下表的「进入 librime 的 modifiers」一列（设未列出的位为 0）。
2. 对每行判断：函数返回 `FALSE` 还是继续往下走？

**预期结果（待本地验证具体位值，逻辑如下）：**

| 用户实际按键 | IBus 给的 `modifiers`（示意） | 经过第一层？ | 传给 librime 的 `modifiers'` |
| --- | --- | --- | --- |
| `Super+e` | `SUPER` 位亮 | **否，return FALSE** | ——（不放行） |
| `Ctrl+a` | `CONTROL` 位亮 | 是 | `CONTROL` |
| `Shift+g` | `SHIFT` 位亮 | 是 | `SHIFT` |
| `Alt+F4` | `MOD1` 位亮 | 是 | `MOD1` |
| 开着 NumLock 按 `5` | `MOD2`（NumLock）位亮 | 是 | **0**（MOD2 被裁剪掉） |

关键观察：NumLock（`MOD2`）被裁剪后，按数字键的 `modifiers'` 是 0，librime 看到的就是「纯数字键」，不会被修饰位干扰；而 `Super+e` 根本进不了 librime。

#### 4.3.5 小练习与答案

**练习 1：** 为什么要同时检测 `IBUS_SUPER_MASK` 和 `IBUS_MOD4_MASK`？只测一个会怎样？

**参考答案：** 因为不同 GTK 版本对 Super 键的标记方式不同：GTK3 倾向用 `IBUS_SUPER_MASK`，GTK4 改用 `IBUS_MOD4_MASK`。只测一个会在另一个环境漏网，导致 Super 快捷键被输入法误吞（表现为「按 Win 键呼不出活动概览」）。两个一起测才能跨 GTK 版本都正确放行。

**练习 2：** 第二层「掩码裁剪」用 `&=` 就地修改了 `modifiers` 参数。这个参数是值传递的，就地修改会影响调用方（IBus）吗？这种写法安全吗？

**参考答案：** 不会影响 IBus。C 语言里函数参数是值传递，`modifiers` 是 `process_key_event` 栈上的副本，`&=` 只改这个副本，IBus 那边的原始事件不受影响。这种写法是安全的，目的是让后续代码（`process_key` 调用）直接用一个干净的名字 `modifiers` 指向裁剪后的值，而不必另起一个变量。

### 4.4 _horizontal 方向同步

#### 4.4.1 概念说明

候选词表可以横排（一行排开）或竖排（一列排开），由全局设置 `lookup_table_orientation` 决定（`IBUS_ORIENTATION_HORIZONTAL` 或 `IBUS_ORIENTATION_VERTICAL`）。这个方向不仅影响「UI 怎么画」，还影响「方向键怎么解释」：

- **竖排**时，候选词是上下排列，所以 `↑`/`↓` 选上一个/下一个候选，`←`/`→` 可能用来翻页或无效；
- **横排**时，候选词是左右排列，所以 `←`/`→` 选上一个/下一个候选，`↑`/`↓` 角色互换。

谁负责「方向键 → 候选导航」的语义？是 librime——因为方向键也走 `process_key`，由核心引擎决定它意味着什么。但 librime 自己并不知道前端把候选表画成了横还是竖，需要前端通过一个会话级选项 `_horizontal` 告诉它。

> 命名约定：librime 的会话选项里，以 `_`（下划线）开头的是「内部选项」，由前端程序化设置；不以 `_` 开头的是「用户选项」，可由方案配置（schema）设置。`_horizontal` 带下划线前缀，属于前者。

#### 4.4.2 核心流程

最朴素的写法是「建会话时设一次 `_horizontal`」——这也正是 1.6.1 之前的老代码。但它有 bug：

```
建会话时设 _horizontal = true
   │
   ├─ 主输入上下文：用户打字，方向键按 horizontal 解释 ✓
   └─ 切换器(switcher)上下文：用户按 F4 呼出方案切换菜单
           └─ switcher 是一个【独立上下文】，不继承主上下文的 _horizontal
           └─ 它的 _horizontal 仍是默认值（false）
           └─ 结果：横排配置下，switcher 里的方向键方向反了 ✗
```

问题根源：librime 的「方案切换器（switcher）」是一个**与主输入上下文分离**的上下文。给主会话设一次 `_horizontal`，不会自动作用于 switcher 上下文。于是当用户在 switcher 菜单里按方向键时，方向是错的。

修复思路（1.6.1，提交 `2927e4d`，issue #232）：**把「设置 `_horizontal`」从「建会话时一次」改成「每次 `process_key` 之前」**。这样无论按键落到主上下文还是 switcher 上下文，处理前都会先把当前会话的 `_horizontal` 校准成最新配置。又为了避免每次按键都无谓地写一遍，加了一个「读出来比一下、不同才写」的守卫。

#### 4.4.3 源码精读

当前代码（修复后）：

[文件 rime_engine.c:533-538](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L533-L538) —— 每次按键前按配置校准 `_horizontal`：

```c
  // Arrow key actions respect candidate list orientation.
  Bool horizontal = g_ibus_rime_settings.lookup_table_orientation ==
                    IBUS_ORIENTATION_HORIZONTAL;
  if (rime_api->get_option(rime_engine->session_id, "_horizontal") != horizontal) {
    rime_api->set_option(rime_engine->session_id, "_horizontal", horizontal);
  }
```

这段做了三件事：

1. 从全局设置读出「是否横排」，转成 `Bool horizontal`；
2. 用 `get_option` 读出当前会话里 `_horizontal` 的实际值，与期望值比较；
3. 仅当两者不一致时才 `set_option`——避免每次按键都写，减少无谓的状态标记。

而修复前的老代码（提交 `2927e4d` 删掉的部分）是这样的：

```c
// 在 ibus_rime_create_session 里（旧版）：
// Define arrow key actions in the selector component.
Bool horizontal = g_ibus_rime_settings.lookup_table_orientation == IBUS_ORIENTATION_HORIZONTAL;
rime_api->set_option(rime_engine->session_id, "_horizontal", horizontal);
```

也就是说，老代码把 `_horizontal` 设在建会话的小助手 [rime_engine.c:89-98](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L89-L98) 里，且**没有 `get_option` 守卫**，每次建会话都直接写。修复把它从这里**删掉**，挪到 `process_key_event` 里并加上守卫。

可以对照提交 `2927e4d` 的 diff 直观看到这次「搬家」：删除 `ibus_rime_create_session` 里设置 `_horizontal` 的 3 行，在 `process_key_event` 里新增 8 行（含守卫）。提交信息写得很清楚：

> Switcher has a separate context from the main Engine for typing, therefore setting "_horizontal" option once per Rime session isn't enough. Set the option before process key (maybe an arrow key).

#### 4.4.4 代码实践

**实践目标：** 解释为什么修复选择了「每次按键前同步 + 守卫」而不是「建会话时同步一次」，并复盘 bug 的复现条件。

**操作步骤：**

1. 用 `git show 2927e4d -- rime_engine.c` 查看修复提交的完整 diff，确认它「从 `create_session` 删除、在 `process_key_event` 新增」。
2. 回答三个问题：
   - 为什么「建会话时设一次」对**主输入**足够，对 **switcher** 不够？
   - 守卫 `if (get_option(...) != horizontal)` 不写，直接每次 `set_option` 会怎样？功能上对吗？
   - 这个同步为什么必须放在 `process_key` **之前**，而不是之后？
3. 在 [rime_engine.c:540-541](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L540-L541) 处确认：`process_key` 紧跟在 `_horizontal` 同步之后。

**预期结果：**

- **主输入 vs switcher**：主输入用的就是建会话时设的那个上下文，所以一次设置对它有效；switcher 是 librime 内部另起的独立上下文，不继承这个设置，必须在它处理按键前重新校准。
- **守卫的作用**：不写守卫、直接每次 `set_option`，**功能上也是对的**（值没变，写进去也一样）。守卫纯粹是优化——避免每次按键都触发一次「会话选项被改」的内部记录，对性能和日志干净度更友好。这是一种「幂等写之前先读」的常见习惯。
- **必须在 `process_key` 之前**：因为 `_horizontal` 影响的是「这次按键里的方向键如何被解释」。如果在 `process_key` 之后才设，那这次的方向键已经按错误的（旧）方向处理完了，校准要等到下一个按键才生效——用户会观察到「第一次按方向键方向反了，第二次才对」。

**待本地验证：** 若想复现老 bug，可把 [rime_engine.c:533-538](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L533-L538) 注释掉，并在 `ibus_rime_create_session` 末尾加回 `rime_api->set_option(rime_engine->session_id, "_horizontal", horizontal);`，重新编译后把 `ibus_rime.yaml` 的 `horizontal` 设为 `true`，呼出方案切换菜单（通常 F4），观察菜单里 `↑`/`↓` 与 `←`/`→` 的导航方向是否符合横排预期。

#### 4.4.5 小练习与答案

**练习 1：** 既然「每次 `set_option`」功能也对，为什么作者还要加 `get_option` 守卫？这是「过度优化」吗？

**参考答案：** 不算过度优化。`set_option` 在 librime 内部会标记会话状态、可能触发日志或回调，每次按键都写会制造无谓的噪声和一点点开销；而一次 `get_option`（读一个布尔值）极廉价。「幂等写之前先读」是处理「频繁事件 + 偶尔变化的状态」的标准模式，既保持正确性又减少副作用，是工程上的好习惯。

**练习 2：** 假设用户在打字过程中动态切换了 `ibus_rime.yaml` 的 `horizontal` 并重新部署，新方向会什么时候生效？为什么不需要重启 IBus？

**参考答案：** 部署会触发 `ibus_rime_load_settings()` 重读配置（u2-l3 讲过 success 分支会再读一次），`g_ibus_rime_settings.lookup_table_orientation` 被更新成新值。之后**下一次按键**进入 `process_key_event` 时，4.4.3 这段代码会读到新的 `horizontal`，与当前会话的 `_horizontal` 不一致，于是 `set_option` 校准成新方向。所以不需要重启 IBus，下一个按键就自然生效——这正是把同步放在「每次按键前」带来的额外好处。

### 4.5 set_option / get_option：运行时开关

#### 4.5.1 概念说明

`set_option(session_id, key, value)` 和 `get_option(session_id, key)` 是 librime 提供的「会话级运行时开关」接口：每个会话有一组字符串键、布尔值的选项，前端可以随时读写。它的作用是**在前端与核心之间传递「无法用按键表达的运行时状态」**。

本项目中用到三个选项，正好代表三种典型用法：

| 选项名 | 类型 | 设置时机 | 作用 |
| --- | --- | --- | --- |
| `soft_cursor` | 内部（无下划线但程序设置） | 建会话时 | 控制预编辑文本里是否显示软光标 |
| `_horizontal` | 内部（下划线前缀） | 每次按键前 | 告知候选表方向，影响方向键语义 |
| `ascii_mode` | 用户选项 | 点击状态栏「中/A」按钮 | 中/英文模式切换 |

`ascii_mode` 是最重要的「用户选项」——它不带下划线前缀，意味着它**可以被方案配置（schema）定义默认值、被 librime 的选项切换器读写**。前端在这里只是「按一下按钮就翻转它」，真正的「中英文模式如何影响查词」是 librime 内部根据这个选项决定的。

#### 4.5.2 核心流程

一次「点击状态栏中/英按钮」的完整数据流：

1. 用户点击状态栏 `InputMode` 按钮，IBus 调用 `property_activate(engine, "InputMode", ...)`；
2. 前端读出当前 `ascii_mode`：`get_option(session_id, "ascii_mode")`；
3. 翻转：`set_option(session_id, "ascii_mode", !当前值)`；
4. 调 `update`，`get_status` 会读出新的 `is_ascii_mode`，状态栏图标随之切换成 `abc.png` 或 `zh.png`（u4-l1）。

这是一条「前端写选项 → 核心读选项改变行为 → 前端读状态刷新 UI」的闭环。`_horizontal` 的流程类似，只是触发时机是「按键前」而非「点击按钮」。

#### 4.5.3 源码精读

`ascii_mode` 的翻转在状态栏回调里：

[文件 rime_engine.c:569-574](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L569-L574) —— 点击「中/A」按钮翻转 `ascii_mode`：

```c
  else if (!strcmp("InputMode", prop_name)) {
    rime_api->set_option(
        rime_engine->session_id, "ascii_mode",
        !rime_api->get_option(rime_engine->session_id, "ascii_mode"));
    ibus_rime_engine_update(rime_engine);
  }
```

这里 `get_option` 和 `set_option` 配合实现「翻转」：因为 librime 没有提供 `toggle_option`，前端只能「先读后写」。注意 `set_option` 的第三个参数是 `!get_option(...)`——C 语言里 `!` 对 `Bool` 取反，干净利落。

`soft_cursor` 的设置在建会话小助手里（已在 4.1.3 引用过）：

[文件 rime_engine.c:93-97](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L93-L97) —— 按预编辑样式决定 `soft_cursor`：

```c
  Bool inline_caret =
      g_ibus_rime_settings.embed_preedit_text &&
      g_ibus_rime_settings.preedit_style == PREEDIT_STYLE_COMPOSITION &&
      g_ibus_rime_settings.cursor_type == CURSOR_TYPE_INSERT;
  rime_api->set_option(rime_engine->session_id, "soft_cursor", !inline_caret);
```

`inline_caret` 是三个配置项的逻辑与——「内联预编辑 + composition 样式 + insert 光标」三者同时成立时，光标由前端自己画（插入点），librime 就不必再画软光标，所以 `soft_cursor = !inline_caret`。这演示了「把多个前端配置合成一个核心选项」的写法。

`_horizontal` 的 get/set 守卫已在 4.4.3 详述，这里不重复。

三个用法放在一起对比，能看到 `set_option`/`get_option` 的两种典型模式：

- **写时读（read-then-write）**：`ascii_mode` 的翻转、`_horizontal` 的守卫，都是「先 `get` 再 `set`」，用于「基于当前值做决策」。
- **一次性写**：`soft_cursor` 在建会话时直接写一个计算好的值，不需要先读。

#### 4.5.4 代码实践

**实践目标：** 理解「选项」作为前端↔核心通信通道的设计，并设计一个假想的新选项。

**操作步骤：**

1. 在 [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) 里搜索 `set_option` 和 `get_option` 的全部出现，按「设置时机」「是否带守卫」「选项名是否带下划线」分类。
2. 假设要新增一个内部选项 `_show_page_num`（是否在候选表显示页码），回答：
   - 应该在「建会话时设一次」还是「每次按键前同步」？为什么？
   - 选项名为什么要带下划线？
   - 写它的代码应该放在哪个函数里？

**预期结果（分类表）：**

| 选项 | 设置点 | 时机 | 守卫 | 下划线前缀 |
| --- | --- | --- | --- | --- |
| `soft_cursor` | `ibus_rime_create_session` | 建会话 | 无 | 否（但程序设置） |
| `_horizontal` | `process_key_event` | 每次按键前 | 有（get 比对） | 是 |
| `ascii_mode` | `property_activate` | 用户点按钮 | 无（直接翻转） | 否（用户选项） |

**关于 `_show_page_num` 的设计：** 如果它只依赖前端配置（像 `soft_cursor` 那样静态），建会话时设一次即可；如果它可能影响按键语义或会被 librime 的独立上下文（switcher）使用（像 `_horizontal`），就应在每次按键前同步并加守卫。带下划线前缀是为了声明「这是前端程序设置的内部选项，不该出现在用户的 schema 配置里」，避免与用户选项命名空间冲突。

**待本地验证：** 若想确认 `ascii_mode` 翻转真的改变了核心行为，可在 [rime_engine.c:569-574](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L569-L574) 的 `set_option` 后加一行 `g_debug("ascii_mode now %d", rime_api->get_option(...));`，重新编译后点击状态栏中/英按钮，观察日志与状态栏图标（`abc.png`↔`zh.png`）是否同步变化。

#### 4.5.5 小练习与答案

**练习 1：** `ascii_mode` 不带下划线前缀，`_horizontal` 带。这个命名差异背后的语义是什么？

**参考答案：** 不带下划线表示「用户选项」——它可以被 librime 的方案配置（schema）定义、被选项切换器读写、对用户可见；前端只是众多读写者之一。带下划线表示「内部选项」——它由前端程序化设置、对用户不可见、不进 schema。`ascii_mode` 属于前者（用户能在方案里配默认中/英文），`_horizontal` 属于后者（纯前端 UI 方向，用户不直接配）。这个约定让两类选项的命名空间分离，互不干扰。

**练习 2：** `property_activate` 里翻转 `ascii_mode` 时没有写「get 守卫」（不像 `_horizontal` 那样先比对再写）。为什么这里不需要守卫？

**参考答案：** 因为这里是「主动翻转」语义——无论当前值是什么，都要写成它的反面，所以必须无条件写，守卫没有意义（即便当前值已经是目标值，那也只是说明用户连点了两下，最终值仍应翻转）。而 `_horizontal` 是「校准」语义——目标是把会话里的值同步成一个固定的期望值，值 already correct 时就不必写，所以需要守卫。两种语义不同，写法也不同。

## 5. 综合实践

把本讲五个最小模块串起来，做一个端到端的「按键旅程追踪」任务：**选一个具体按键场景，画出从 IBus 到 librime 再到 UI 的完整时序，并标注每一道关卡的作用。**

> 说明：本任务是「源码阅读 + 推演型实践」，不要求你真的修改并编译运行（那会动到源码，超出本讲范围）。请以**阅读 + 画图**的方式完成，重点是把五块知识串成一条链。

**场景选择（任选其一，建议都推演一遍）：**

- **场景 A：部署后第一次敲字母键。** 用户刚点了「部署」按钮（触发 `ibus_rime_stop` + `ibus_rime_start`），然后按了 `g` 键。
- **场景 B：横排配置下按方向键选词。** `ibus_rime.yaml` 设 `horizontal: true`，用户输入拼音后按 `→` 选下一个候选。
- **场景 C：开着 CapsLock 按字母键。** 系统开着 CapsLock，用户按 `g`。

**操作步骤：**

1. 对照 [rime_engine.c:509-544](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L509-L544) 的 `process_key_event`，画出一张时序图，至少包含这些角色：`IBus`、`process_key_event`、`find_session/create_session`、`get_option/set_option`、`rime_api->process_key`、`ibus_rime_engine_update`、`librime 核心`。
2. 在图上标注每一步「如果删掉会发生什么 bug」，对应本讲哪一节。
3. 对三个场景分别回答：
   - 场景 A：`find_session` 返回什么？为什么？（4.1）
   - 场景 B：`_horizontal` 守卫里 `get_option` 与 `horizontal` 的关系？为什么必须在 `process_key` 前？（4.4）
   - 场景 C：`modifiers` 经过两层过滤后变成什么？`CapsLock` 位会被保留还是裁掉？（4.3）

**预期结果：** 你应当得到一张统一的时序图，三个场景只是「在不同关卡触发不同分支」：A 在「会话保证」关卡触发重建，B 在「方向同步」关卡触发（或不触发）写入，C 在「修饰键过滤」关卡被裁剪。这正好说明 `process_key_event` 是一道**职责清晰、各关独立的流水线**——这也是后续给按键处理做任何扩展（新增修饰键规则、新增方向选项、新增会话恢复策略）时的改造落脚点。

## 6. 本讲小结

- `RimeSessionId` 是 librime 一次输入过程的全部状态句柄；`create_session`（[rime_engine.c:92](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L92-L92)）建、`find_session`（[rime_engine.c:525](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L525-L525)）查、`destroy_session`（[rime_engine.c:159](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L159-L159) 与 [rime_engine.c:225](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L225-L225)）销毁；部署会重启 librime 使旧会话失效，所以按键前必须 `find_session` 防御。
- `ibus_rime_engine_process_key_event`（[rime_engine.c:509-544](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L509-L544)）是一道八步流水线：过滤 Super → cast → 裁剪掩码 → 找回会话 → 同步方向 → 转交按键 → 刷新 UI → 返回；它本身不含业务逻辑，只做编排，体现「薄前端」。
- 修饰键过滤分两层（[rime_engine.c:516-523](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L516-L523)）：`IBUS_SUPER_MASK | IBUS_MOD4_MASK` 整键放行（`return FALSE`，跨 GTK3/4），再把掩码裁剪到 `RELEASE|LOCK|SHIFT|CONTROL|MOD1` 五位传给 librime。
- `_horizontal` 必须在每次 `process_key` 前同步（[rime_engine.c:533-538](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L533-L538)），因为方案切换器是独立上下文、不继承建会话时的设置；这是 1.6.1（提交 `2927e4d`）的 arrow key orientation 修复，并带 `get_option` 守卫减少无谓写入。
- `set_option` / `get_option` 是前端↔核心的运行时开关通道：`soft_cursor`（建会话设）、`_horizontal`（按键前同步）、`ascii_mode`（点按钮翻转，[rime_engine.c:569-574](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L569-L574)）分别代表「一次性写」「校准式写」「翻转式写」三种模式，下划线前缀区分内部选项与用户选项。

## 7. 下一步学习建议

本讲把「按键如何送到 librime」讲透了，但按键处理完之后，librime 的新状态如何变成屏幕上的预编辑文本、候选词表、状态栏图标——这部分被 `process_key_event` 末尾那一句 `ibus_rime_engine_update(rime_engine)` 故意「打包委托」了，正是 u4 的主题。建议接下来：

- **u3-l3 引擎回调与状态栏操作**：精读 `focus_in`/`focus_out`/`reset`/`enable`/`disable`/`property_activate`（[rime_engine.c:186-575](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L186-L575)），看本讲里反复出现的「部署」「同步」「中/英切换」三个状态栏按钮是如何被点击触发的，并补全 `disable` 为何要销毁会话、`reset` 为何要 `clear_composition`。
- **u4-l1 综合更新函数与状态同步**：精读 `ibus_rime_engine_update`（[rime_engine.c:283-507](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L283-L507)），看本讲每次按键末尾调用的这个函数，如何依次拉取 `RimeStatus`/`RimeCommit`/`RimeContext` 三段数据并刷到 UI。
- **u4-l3 候选词列表渲染**：看本讲里被反复提及的 `_horizontal`/`lookup_table_orientation` 最终如何驱动 `IBusLookupTable` 的方向（[rime_engine.c:496-497](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L496-L497)），把「方向选项」从配置一路追到屏幕上的横排/竖排。
- 若想再巩固 librime 会话与选项的概念，可阅读 librime 的 `rime_api.h`（在 `librime/` 子模块里）中 `RimeApi` 结构体对 `create_session`/`find_session`/`destroy_session`/`process_key`/`set_option`/`get_option` 的注释，对照本讲的行为描述加深理解。
