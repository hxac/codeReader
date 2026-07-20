# 综合更新函数与状态同步

## 1. 本讲目标

在前几讲里，我们已经知道按键、状态栏按钮最终都不会「直接」去改 UI，而是改完 librime 的选项或状态后，统一调用一个函数 `ibus_rime_engine_update` 把变化「投影」到 IBus 界面上。本讲就专门拆解这个投影函数。

学完本讲，你应当能够：

- 说清 `ibus_rime_engine_update` 的**三段式结构**：先拉状态（status）、再拉提交（commit）、最后拉上下文（context）。
- 解释 `ibus_rime_update_status` 如何用**去重判断**避免无谓的状态栏刷新，以及在 `disabled`、`ascii`、中文三种模式下分别显示什么图标与符号。
- 理解 `get_commit` 与 `ibus_engine_commit_text` 的时机：为什么提交文本要在渲染候选表之前单独处理。
- 把本讲当作 U4 单元的「总纲」——后面的预编辑文本（u4-l2）和候选表（u4-l3）都是在 context 这一段里展开的。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（来自前置讲义）：

- **薄前端 / RimeApi 稳定边界**（u2-l3、u3-l1）：ibus-rime 不查词，所有真正的输入法逻辑都在 librime 里，前端只通过全局 `rime_api` 指针（`RimeApi*`）调用它的函数。
- **会话 `RimeSessionId`**（u3-l2）：每次 `create_session` 得到一个会话句柄，后续所有 `get_status` / `get_commit` / `get_context` 都要带上它。部署（deploy）会重启 librime，旧会话句柄会失效。
- **GObject 与 IBusEngine 虚函数**（u3-l1）：`IBusRimeEngine` 继承自 `IBusEngine`，实例结构体里持有 `session_id`、`status`、`table`、`props` 四个成员。
- **状态栏三按钮**（u3-l3）：`props` 列表里第 0 个属性是 `InputMode`（中英文切换），它正是本讲状态栏图标刷新的「主角」。

本讲会用到几个 IBus / librime 的基本类型，先在此做个最小介绍：

| 类型 / 宏 | 来源 | 作用 |
| --- | --- | --- |
| `RimeStatus` | librime | 描述「当前是否禁用、是否英文、用哪个方案」 |
| `RimeCommit` | librime | 描述「本次按键要上屏的成字文本」 |
| `RimeContext` | librime | 描述「当前预编辑串、选中区间、候选菜单」 |
| `RIME_STRUCT(T, v)` | librime | 在栈上声明一个结构体并写入大小字段（跨版本 ABI 兼容） |
| `IBusProperty` | ibus | 状态栏上的一个按钮（图标 + 标签 + 符号） |
| `IBusLookupTable` | ibus | 候选词表（本讲只用到它的清空，详情见 u4-l3） |

> 名词速查：**投影（projection）**——指把 librime 内部状态「翻译并下发」成 IBus 能显示的 UI 原语。本讲的 `update` 就是一次完整的投影。

## 3. 本讲源码地图

本讲几乎全部内容集中在**一个文件**里：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `rime_engine.c` | 引擎层，定义 `IBusRimeEngine` 类型并实现所有按键/回调/UI 渲染 | `ibus_rime_engine_update`（主投影函数）、`ibus_rime_update_status`（状态去重与图标切换） |
| `rime_engine.h` | 暴露 `IBUS_TYPE_RIME_ENGINE` 宏与 `get_type` 声明 | 仅为引用，本讲不深入 |
| `rime_settings.h` | 全局设置结构 `g_ibus_rime_settings` 与颜色/样式枚举 | context 段会用到 `embed_preedit_text`、`preedit_style` 等字段，本讲只点到为止 |

本讲的「主角」是这两个函数：

- `ibus_rime_engine_update`（[rime_engine.c:283-507](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L283-L507)）——三段式总入口。
- `ibus_rime_update_status`（[rime_engine.c:230-281](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L230-L281)）——状态去重与状态栏图标切换。

> 提醒：context 段的后半部分（预编辑文本、候选表构造）非常长，但它们属于 u4-l2 与 u4-l3 的范畴。本讲对 context 只讲「入口的早退判断」，把详细渲染留给后续两讲，以免一次塞进太多内容。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **三段式更新主流程**：`get_status` / `get_commit` / `get_context`。
2. **`update_status` 的状态去重**：什么情况下「直接 return 不刷新」。
3. **状态栏图标三态切换**：`disabled` / `abc` / `zh` 与符号投影。
4. **提交文本的提取与下发**：`get_commit` 与 `ibus_engine_commit_text`。

### 4.1 三段式更新主流程（get_status / get_commit / get_context）

#### 4.1.1 概念说明

在 IBus 的架构里，输入法引擎（engine）和桌面输入法框架之间是**事件驱动**的：按键来了、焦点变了、按钮点了，框架都会回调引擎的某个虚函数。但对 ibus-rime 来说，这些回调无论哪一个，最后都汇聚到同一个出口——`ibus_rime_engine_update`。

这背后的设计直觉是：**librime 是「真相之源」（single source of truth）**。前端不自己维护「当前是不是英文模式」「当前预编辑串是什么」，这些一律以 librime 给出的为准。所以每次外部事件发生后，前端只需要做一件事——把 librime 此刻的状态**重新拉一遍、刷一遍 UI**。

「拉一遍」具体拉什么？librime 把会话的即时状态分成三块独立的结构体：

- `RimeStatus`：**模式级**状态——是否被禁用、是否英文模式、当前用的是哪个输入方案。
- `RimeCommit`：**本次待上屏**的成字文本（一次按键可能产生一个要直接输入到应用里的字串）。
- `RimeContext`：**当前编辑现场**——预编辑串、选中高亮区间、候选菜单。

这三块彼此独立、各自有自己的 `get_xxx` / `free_xxx` 配对，因此 `update` 自然写成三段。

#### 4.1.2 核心流程

`ibus_rime_engine_update` 的骨架可以概括为下面这段伪代码：

```
function ibus_rime_engine_update(rime_engine):
    # 第一段：状态（驱动状态栏图标）
    if rime_api->get_status(session_id, &status):
        ibus_rime_update_status(rime_engine, &status)   # 有状态：正常去重刷新
        rime_api->free_status(&status)
    else:
        ibus_rime_update_status(rime_engine, NULL)      # 无状态：按"禁用"处理

    # 第二段：提交文本（驱动上屏）
    if rime_api->get_commit(session_id, &commit):
        ibus_engine_commit_text(engine, 用 commit.text 造的 IBusText)
        rime_api->free_commit(&commit)

    # 第三段：上下文（驱动预编辑/辅助/候选表）
    if not get_context(...) or composition 为空:
        隐藏 预编辑 / 辅助文本 / 候选表
        free_context; return            # ← 注意这里直接返回，不再往下渲染
    # ... 预编辑文本、辅助文本、候选表的详细渲染（u4-l2、u4-l3）
```

三个关键点先记住：

1. **三段的顺序是固定的**：status → commit → context。状态栏最先刷新，提交文本居中，UI 渲染最后。
2. **每个 `get_xxx` 都配一个 `free_xxx`**：这些结构体里可能含 librime 内部 `malloc` 的字符串（如 `schema_id`、`commit.text`），用完必须释放，否则会内存泄漏。
3. **context 段有一个「早退」分支**：当没有上下文或预编辑串为空时，隐藏所有 UI 并 `return`，不进入后面的渲染逻辑。

#### 4.1.3 源码精读

下面是 `ibus_rime_engine_update` 的开头——也就是本模块关心的三段式入口（context 段只截到早退判断为止）：

```c
static void ibus_rime_engine_update(IBusRimeEngine *rime_engine)
{
  // update properties
  RIME_STRUCT(RimeStatus, status);
  if (rime_api->get_status(rime_engine->session_id, &status)) {
    ibus_rime_update_status(rime_engine, &status);
    rime_api->free_status(&status);
  }
  else {
    ibus_rime_update_status(rime_engine, NULL);
  }
  ...
}
```

> 见 [rime_engine.c:283-293](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L283-L293)。第一段：用 `RIME_STRUCT` 在栈上声明 `status`，`get_status` 成功就把状态交给 `ibus_rime_update_status` 刷新状态栏并 `free_status`；失败则传 `NULL`（语义是「服务不可用」，进入 disabled 分支）。

`RIME_STRUCT(RimeStatus, status)` 是 librime 提供的宏，展开后大致等价于「声明结构体 + 把 `size` 字段写成 `sizeof(RimeStatus)`」。这个 `size` 字段是 librime 跨版本兼容的关键：新版 librime 如果给结构体加了字段，旧前端只要写对 `size`，librime 就知道「这个前端只认前 N 个字段」，从而安全地读写。

第二段（commit）与第三段的早退判断：

```c
  // commit text
  RIME_STRUCT(RimeCommit, commit);
  if (rime_api->get_commit(rime_engine->session_id, &commit)) {
    IBusText *text;
    text = ibus_text_new_from_string(commit.text);
    // the text object will be released by ibus
    ibus_engine_commit_text((IBusEngine *)rime_engine, text);
    rime_api->free_commit(&commit);
  }

  // begin updating UI
  RIME_STRUCT(RimeContext, context);
  if (!rime_api->get_context(rime_engine->session_id, &context) ||
      context.composition.length == 0) {
    ibus_engine_hide_preedit_text((IBusEngine *)rime_engine);
    ibus_engine_hide_auxiliary_text((IBusEngine *)rime_engine);
    ibus_engine_hide_lookup_table((IBusEngine *)rime_engine);
    rime_api->free_context(&context);
    return;
  }
  ... // 预编辑 / 辅助文本 / 候选表（见 u4-l2、u4-l3）
}
```

> 见 [rime_engine.c:295-315](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L295-L315)。

第三段的早退条件 `!get_context(...) || composition.length == 0` 很重要：意思是「**取不到上下文**」或「**预编辑串是空的**」都要把三类 UI 全部隐藏并返回。这对应了用户刚提交完一个字、或还没开始打字时的状态——此时没有候选、没有预编辑，界面应当是干净的。注意即便早退，`free_context(&context)` 依然要调（get_context 失败时它内部通常是空操作，但调用是安全的）。

> **谁会触发 update？** 在 [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) 中，`ibus_rime_engine_update` 共有 10 个调用点：`focus_in`（L193）、`reset`（L211）、`process_key_event`（L529 服务禁用分支、L542 正常按键后）、`property_activate` 的 deploy/sync/InputMode 三分支（L556/L567/L573）、`candidate_clicked`（L594）、`page_up`/`page_down`（L602/L609）。也就是说，几乎所有外部事件都以「调一次 update」收尾——这正是「投影」模式的力量：入口很多，出口只有一个。

#### 4.1.4 代码实践

**实践目标**：在源码里确认「三段顺序」与「每段都配 free」，并理解 context 早退分支的意义。

**操作步骤**：

1. 打开 [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c)，定位到 `ibus_rime_engine_update`（L283）。
2. 用三种颜色的高亮笔/注释分别标注：第一段 status（L286-L293）、第二段 commit（L296-L303）、第三段 context 入口与早退（L307-L315）。
3. 在每段里圈出成对出现的 `get_xxx` / `free_xxx`。
4. 思考：如果把第二段的 `rime_api->free_commit(&commit)` 删掉，会发生什么？

**需要观察的现象 / 预期结果**：

- 三段顺序为 status → commit → context，且 status 段一定在 commit 之前。
- 每个成功 `get_xxx` 的分支里都能找到对应的 `free_xxx`。
- 删掉 `free_commit` 后，每次有字上屏都会泄漏 `commit.text` 这段字符串（**待本地验证**：可在 `get_commit` 成功分支里临时加一行 `g_message("commit len=%d", (int)strlen(commit.text));` 观察日志，确认每次按键都触发）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_status` 失败时，代码选择传 `NULL` 给 `update_status`，而不是直接跳过状态栏刷新？

**参考答案**：因为「取不到状态」本身就是一个有意义的状态——它通常意味着会话已失效或服务被禁用（例如部署刚重启 librime、或 `session_id` 失效）。传 `NULL` 让 `update_status` 走 disabled 分支，把图标显示成「維護」，向用户诚实地反馈「现在用不了」。如果直接跳过，状态栏会停留在上一次的图标上，给用户「还能用」的错觉。

**练习 2**：第三段 context 的早退条件是「或」`||` 连接的两个判断，分别对应什么用户场景？

**参考答案**：`!get_context(...)` 对应「会话无效，librime 拿不到上下文」；`composition.length == 0` 对应「会话有效，但用户当前没有正在编辑的内容」（比如刚上屏一个字、或还没开始打）。两种情况下 UI 都应该是空的，所以共用同一个隐藏分支。

---

### 4.2 update_status 的状态去重

#### 4.2.1 概念说明

状态栏图标刷新有一个现实问题：按键事件非常频繁，每按一个键都会触发一次 `update`，进而触发一次 `get_status` + `update_status`。但「模式级状态」其实变化极少——你可能连续敲了二十个拼音字母，全程都是「中文模式 + 朙月拼音」，这二十次 `get_status` 返回的 `RimeStatus` 几乎完全一样。

如果每次都无脑地 `set_icon` + `set_label` + `update_property`，不仅浪费，还可能让状态栏图标频繁闪烁、增加 IBus 与前端之间的 D-Bus 往返。所以 `ibus_rime_update_status` 在最开头加了一段**去重判断**：如果新旧状态完全一致，就直接 `return`，什么都不做。

去重比较的是 `RimeStatus` 里**影响状态栏显示的三个字段**：

- `is_disabled`：是否被禁用（决定是否显示「維護」图标）。
- `is_ascii_mode`：是否英文模式（决定显示「Abc」还是中文方案名）。
- `schema_id`：当前输入方案的标识字符串（决定中文模式下显示哪个方案名）。

#### 4.2.2 核心流程

去重的判断逻辑可以写成：

```
if status != NULL
   and 旧.is_disabled     == 新.is_disabled
   and 旧.is_ascii_mode   == 新.is_ascii_mode
   and 旧.schema_id != NULL and 新.schema_id != NULL
   and strcmp(旧.schema_id, 新.schema_id) == 0:
      return                 # 完全没变，什么都不做
# 否则：把新字段拷到旧字段，刷新图标/标签
```

关键细节：

1. **去重是「短路与」**：所有条件必须**同时**成立才 return；只要有一个不成立，就继续往下做真正的刷新。
2. **schema_id 必须两边都非空**：因为下一步要用 `strcmp`，传 `NULL` 给 `strcmp` 是未定义行为，所以先用 `旧.schema_id && 新.schema_id` 守护。
3. **这也意味着**：当 `status == NULL`（get_status 失败）时，第一个条件就为假，去重被跳过，必然进入刷新流程——这与 4.1 里「失败要显示維护」的设计一致。

#### 4.2.3 源码精读

```c
static void ibus_rime_update_status(IBusRimeEngine *rime_engine,
                                    RimeStatus *status)
{
  if (status &&
      rime_engine->status.is_disabled == status->is_disabled &&
      rime_engine->status.is_ascii_mode == status->is_ascii_mode &&
      rime_engine->status.schema_id && status->schema_id &&
      !strcmp(rime_engine->status.schema_id, status->schema_id)) {
    // no updates
    return;
  }

  rime_engine->status.is_disabled = status ? status->is_disabled : False;
  rime_engine->status.is_ascii_mode = status ? status->is_ascii_mode : False;
  if (rime_engine->status.schema_id) {
    g_free(rime_engine->status.schema_id);
  }
  rime_engine->status.schema_id =
      status && status->schema_id ? g_strdup(status->schema_id) : NULL;
  ... // 后面是图标/标签切换（见 4.3）
}
```

> 见 [rime_engine.c:230-248](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L230-L248)。

注意 `rime_engine->status` 是引擎实例里**常驻**的 `RimeStatus` 成员（在 `init` 里用 `RIME_STRUCT_INIT` + `RIME_STRUCT_CLEAR` 初始化，见 [rime_engine.c:105-106](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L105-L106)），它保存的是「上一次刷新后」的状态。函数参数 `status` 则是「这一次 `get_status` 拿到」的栈上副本。去重就是拿这两份做对比。

字段拷贝有两点值得学：

- `is_disabled` / `is_ascii_mode` 是普通布尔值，直接赋值即可。
- `schema_id` 是**字符串**：旧的要先 `g_free`（释放上一次 `g_strdup` 出来的拷贝），新的再用 `g_strdup` 复制一份 owned 拷贝存起来。这正是 u3-l1 讲过的「owned 字符串用 `g_free`」原则。注意 librime 传进来的 `status->schema_id` 生命周期属于 librime，前端不能直接存它的指针，必须自己复制一份。

#### 4.2.4 代码实践

**实践目标**：用纸笔或注释把「去重 return」的五个条件列全，并构造出「会 return」和「不会 return」的输入。

**操作步骤**：

1. 定位 [rime_engine.c:233-240](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L233-L240) 的去重 `if`。
2. 列表写出全部 5 个条件（status 非空、is_disabled 等、is_ascii_mode 等、两个 schema_id 都非空、strcmp 为 0）。
3. 填下面这张「是否会 return」的真值表。

**需要观察的现象 / 预期结果**（请自行补全空格）：

| 场景 | status | is_disabled 变了? | is_ascii_mode 变了? | schema_id | 去重 return? |
| --- | --- | --- | --- | --- | --- |
| 连续打拼音，模式未变 | 非 NULL | 否 | 否 | 两边相等且非空 | **会 return** |
| 刚切到英文模式 | 非 NULL | 否 | 是 | … | 不会 |
| 会话失效 | NULL | — | — | — | 不会（status 假） |
| 首次进入，旧 schema_id 还是 NULL | 非 NULL | 否 | 否 | 新非空、旧 NULL | 不会（旧 schema_id 假） |
| 切换了输入方案 | 非 NULL | 否 | 否 | strcmp 非 0 | 不会 |

**预期结果**：你应当发现「首次进入」永远**不会**去重——因为初次时 `rime_engine->status.schema_id` 还是 NULL，第四个条件为假。这保证第一次一定会把图标刷出来。

#### 4.2.5 小练习与答案

**练习 1**：去重条件里为什么要单独写 `rime_engine->status.schema_id && status->schema_id`，而不是直接 `!strcmp(...)`？

**参考答案**：因为 `strcmp` 的两个参数都不能是 `NULL`，否则是未定义行为（很可能段错误）。首次刷新时旧的 `schema_id` 还是 NULL，必须先用 `&&` 把「两边都非空」这个前置条件卡住，短路求值保证 `strcmp` 只在安全时才被执行。

**练习 2**：假设有人把去重判断整段删掉，每次都无条件刷新状态栏，功能上还能用吗？有什么坏处？

**参考答案**：功能上仍然正确（最终显示的状态一样），但坏处是：每次按键都会多一次 `g_free` + `g_strdup`（无谓的字符串复制）和一次 `ibus_engine_update_property`（跨进程 D-Bus 调用），在连续打字时既浪费 CPU/内存，又增加与 ibus 守护进程的通信开销，极端情况下可能造成状态栏图标闪烁。

---

### 4.3 状态栏图标三态切换（disabled / abc / zh）

#### 4.3.1 概念说明

去重通过后，`update_status` 真正要做的事是：根据当前状态，更新状态栏第 0 个属性（`InputMode` 按钮）的**图标**、**标签**和**符号**，然后调用 `ibus_engine_update_property` 把改动下发。

这里有一个「三态」的判断结构，对应输入法的三种宏观模式：

| 模式 | 触发条件 | 图标文件 | 标签文字 | 含义 |
| --- | --- | --- | --- | --- |
| 禁用 / 维护 | `status == NULL` 或 `is_disabled` | `disabled.png` | `維護` | 服务不可用 |
| 英文（ASCII） | `is_ascii_mode` 为真 | `abc.png` | `Abc` | 直出 ASCII，不查词 |
| 中文 | 其它情况 | `zh.png` | 方案名或 `中文` | 正常中文输入 |

图标文件都位于编译期宏 `IBUS_RIME_ICONS_DIR` 指定的目录下（见 u1-l2），仓库 `icons/` 里确实存在 `disabled.png`、`abc.png`、`zh.png` 三个文件。

#### 4.3.2 核心流程

```
prop = props 列表里第 0 个属性（即 InputMode）
if status 为空或 is_disabled:
    icon = disabled.png ; label = "維護"
elif is_ascii_mode:
    icon = abc.png      ; label = "Abc"
else:  # 中文模式
    icon = zh.png
    if schema_name 存在且不以 '.' 开头:
        label = schema_name           # 显示方案名，如 "朙月拼音"
    else:
        label = "中文"                 # switcher 上下文里方案名是 ".default"

# 用 label 的第一个字符作为"符号"（symbol）
if 非禁用 且 label 非空:
    symbol = label 的第一个 Unicode 字符
    set_symbol(prop, symbol)
set_icon(prop, icon)
set_label(prop, label)
ibus_engine_update_property(engine, prop)   # 下发给 IBus 刷新
```

两个值得注意的设计：

1. **schema_name 的「点」过滤**：在方案切换器（switcher）上下文里，`schema_name` 会是 `".default"` 这类以 `.` 开头的内部占位串，不能直接显示给用户，所以遇到 `.` 开头就退回到默认的 `中文` 字样。
2. **symbol 的提取**：`symbol` 是 IBus 属性里专门的一个字段，许多桌面环境会在输入法指示器（顶部状态栏）上用这个单字符来紧凑地表示当前输入法（比如顶部菜单栏只显示一个「中」或「A」）。它取自 `label` 的第一个 Unicode 字符——所以 `中文` → `中`、`Abc` → `A`、`維護` → `維`。

#### 4.3.3 源码精读

```c
  IBusProperty* prop = ibus_prop_list_get(rime_engine->props, 0);
  const gchar* icon;
  IBusText* label;
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
    if (status && !status->is_disabled && ibus_text_get_length(label) > 0) {
      gunichar c = g_utf8_get_char(ibus_text_get_text(label));
      IBusText* symbol = ibus_text_new_from_unichar(c);
      ibus_property_set_symbol(prop, symbol);
    }
    ibus_property_set_icon(prop, icon);
    ibus_property_set_label(prop, label);
    ibus_engine_update_property((IBusEngine *)rime_engine, prop);
  }
```

> 见 [rime_engine.c:250-280](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L250-L280)。

逐句拆解：

- `ibus_prop_list_get(rime_engine->props, 0)`：从属性列表里取第 0 个，也就是 `init` 时最先 `append` 进去的 `InputMode` 属性（见 [rime_engine.c:114-128](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L114-L128)，它在 `init` 里初始图标就是 `zh.png`、初始 label 是 `中文`）。`if (prop)` 是防御性判空。
- 三分支 `if / else if / else`：注意第一个分支的条件是 `!status || status->is_disabled`，把「取不到状态」和「被显式禁用」合并成同一种「维护」表现——这正是 4.1 里传 `NULL` 的最终落脚点。
- `ibus_text_new_from_static_string` vs `ibus_text_new_from_string`：前者用于字符串字面量（如 `"維護"`、`"中文"`，生命周期随进程，不需要复制）；后者用于「不属于自己」的字符串（如 `status->schema_name`，librime 随时可能释放，所以 IBusText 会内部 `g_strdup` 一份）。这是 GObject 体系里常见的「static / copy」二选一。
- symbol 提取：`g_utf8_get_char` 取 UTF-8 串的第一个码点，`ibus_text_new_from_unichar` 把它包成 `IBusText`。条件里特意排除了禁用态（`!status->is_disabled`），所以「維護」态不会把「維」推到顶部指示器。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把三态分支与符号投影彻底走一遍，能闭着眼说出每种模式下状态栏显示什么。这是本讲规格里要求的实践。

**操作步骤**：

1. 打开 [rime_engine.c:253-280](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L253-L280)，对照下表逐行核验。
2. 在 `ibus_property_set_icon` 那一行（L277）前临时加一行调试日志（**示例代码**，仅用于本地观察，不改业务）：
   ```c
   // 示例代码：调试用，确认实际进入哪个分支
   g_message("[rime-status] disabled=%d ascii=%d schema=%s icon=%s",
             status ? status->is_disabled : -1,
             status ? status->is_ascii_mode : -1,
             (status && status->schema_id) ? status->schema_id : "(null)",
             icon);
   ```
3. 重新编译运行（参考 u1-l2 的 `make`），分别触发三种状态：
   - **disabled**：在用户目录里把方案配置弄失效或触发部署期间观察。
   - **ascii**：点状态栏「中↔A」按钮，或按切换英文的快捷键。
   - **中文**：正常中文模式，并切换到某个具体方案（如朙月拼音）。

**需要观察的现象 / 预期结果**：

| 模式 | 图标文件 | label | 顶部指示器 symbol | 日志里 `icon` 取值 |
| --- | --- | --- | --- | --- |
| 禁用/维护 | `disabled.png` | `維護` | 不设置（保持原值） | `.../disabled.png` |
| 英文 | `abc.png` | `Abc` | `A` | `.../abc.png` |
| 中文（默认/switcher） | `zh.png` | `中文` | `中` | `.../zh.png` |
| 中文（具体方案，如朙月拼音） | `zh.png` | `朙月拼音`（方案名） | `朙` | `.../zh.png` |

> 注：symbol 的实际显示取决于桌面环境（GNOME/KDE）是否读取 `IBusProperty` 的 symbol 字段，部分环境只显示 icon。**待本地验证**你所用桌面的具体表现。

**关于「去重 return 的条件」**（规格要求）：去重 return 当且仅当 [rime_engine.c:233-239](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L233-L239) 的五个条件**同时成立**——`status` 非空、`is_disabled` 未变、`is_ascii_mode` 未变、两边 `schema_id` 都非空且字符串相等。只要其中任一不满足（含「首次进入旧 schema_id 为 NULL」「切了方案」「切了中英文」「服务失效 status 为 NULL」），就**不会** return，而是继续往下刷新图标。因此「三态切换」只在真正发生变化时才执行。

#### 4.3.5 小练习与答案

**练习 1**：在 switcher（方案切换器）上下文里，`schema_name` 是 `".default"`。这时 label 会显示什么？为什么需要特判？

**参考答案**：会显示 `中文`（走 `else` 分支），而不是 `.default`。需要特判是因为 `.default` 是 librime 内部的占位串（表示「还没选定具体方案」），直接显示给用户毫无意义且令人困惑，所以凡是以 `.` 开头的 schema_name 都回退到通用的 `中文` 字样。

**练习 2**：为什么 symbol 提取的条件里有 `!status->is_disabled`？也就是说，「維護」态为什么不让顶部指示器显示「維」？

**参考答案**：这是产品取舍。禁用/维护是一种「异常态」，此时输入法实际不可用，不应该让顶部指示器显示一个像「維」这样容易被误解为「某个名叫維的方案」的字符。条件里排除禁用态，意味着维护时只通过 `disabled.png` 图标表达「不可用」，而不污染顶部指示器。

---

### 4.4 提交文本的提取与下发（get_commit 与 ibus_engine_commit_text）

#### 4.4.1 概念说明

第二段 commit 处理的是「**上屏**」这件事。当用户打字过程中 librime 决定「现在可以输出一段确定的文字到应用里」时（比如敲完拼音选了候选词、或者敲了一个标点），librime 会在会话里产生一个 `RimeCommit`，里面带着要上屏的文本。

「上屏」和「预编辑」是两个不同的概念，初学者很容易混淆：

- **预编辑（preedit）**：还在编辑中、**没确定**的内容，显示在应用光标处的内联文本或浮动的候选窗里，用户还能改。属于 context 段。
- **提交（commit）**：**已确定**、要真正插入到应用文档里的文字，一旦提交就「落袋为安」。属于 commit 段。

IBus 对应的 API 是 `ibus_engine_commit_text(engine, text)`，它会把文本作为「已提交」内容发给当前焦点应用，由应用把它插入到文档（比如你正在编辑的文本框）。

#### 4.4.2 核心流程

```
RIME_STRUCT(RimeCommit, commit)
if rime_api->get_commit(session_id, &commit):
    text = ibus_text_new_from_string(commit.text)   # 拷贝一份给 IBus
    ibus_engine_commit_text(engine, text)           # 下发给应用，真正"上屏"
    rime_api->free_commit(&commit)                  # 释放 librime 分配的内存
# 注意：即使没有 commit，也不需要"隐藏"什么——commit 是一次性事件
```

两个要点：

1. **commit 是「一次性」的**：一次按键最多产生一个 commit；没有 commit 就什么都不做，不存在「隐藏上次 commit」的概念（已提交的文本归应用管）。
2. **commit 必须在 context 渲染之前处理**：因为同一次按键可能既产生 commit（上屏一个字）又产生新的 context（继续编辑下一个字）。先上屏、再画新的预编辑，顺序才符合用户直觉。

#### 4.4.3 源码精读

```c
  // commit text
  RIME_STRUCT(RimeCommit, commit);
  if (rime_api->get_commit(rime_engine->session_id, &commit)) {
    IBusText *text;
    text = ibus_text_new_from_string(commit.text);
    // the text object will be released by ibus
    ibus_engine_commit_text((IBusEngine *)rime_engine, text);
    rime_api->free_commit(&commit);
  }
```

> 见 [rime_engine.c:296-303](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L296-L303)。

几个细节：

- `ibus_text_new_from_string`（而非 `_static_string`）：因为 `commit.text` 的内存归 librime 管，紧接着的 `free_commit` 会把它释放掉，所以 `IBusText` 必须自己 `g_strdup` 一份副本，不能只存指针。
- 注释 `// the text object will be released by ibus`：把 `text` 交给 `ibus_engine_commit_text` 后，**所有权转移给 IBus**，前端不需要、也不应该再 `g_object_unref` 它。这是 IBus API 的一个所有权约定，记住即可。
- `rime_api->free_commit(&commit)`：释放 librime 在 `commit` 里分配的资源（如 `commit.text`）。必须配对调用。

> **时机小结**：`get_commit` 成功意味着「这次按键有字要上屏」。它紧跟着 status 段、先于 context 段，保证「状态栏先更新 → 字先上屏 → 再画新的预编辑/候选」这个符合直觉的顺序。

#### 4.4.4 代码实践

**实践目标**：确认 commit 的所有权流转，并理解它与预编辑的区别。

**操作步骤**：

1. 定位 [rime_engine.c:296-303](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L296-L303)。
2. 在 `ibus_engine_commit_text` 之前临时加日志（**示例代码**）：
   ```c
   // 示例代码：观察每次上屏内容
   g_message("[rime-commit] '%s'", commit.text);
   ```
3. 重新运行，用拼音输入一个完整字（比如敲 `ni` 选「你」），观察日志。

**需要观察的现象 / 预期结果**：

- 选词确认的那一刻，日志会打印出 `[rime-commit] '你'`，且这个字会插入到应用文档里。
- 如果你只是敲了 `n`、`i` 还没选词，**不会**触发 commit 日志——此时只有预编辑（context）在更新，候选窗在动，但没有任何字上屏。这正好说明 commit 与 preedit 的区别。
- **待本地验证**：连续输入一句话，观察 commit 是「逐字触发」还是「整句触发」，这取决于所用方案是否开启整句模式。

#### 4.4.5 小练习与答案

**练习 1**：为什么这里用 `ibus_text_new_from_string`，而 4.3 里 `中文`、`維護` 等用 `ibus_text_new_from_static_string`？

**参考答案**：`commit.text` 的生命周期由 librime 控制，紧接着的 `free_commit` 会释放它，所以 `IBusText` 必须自己复制一份（用 `_from_string`，内部 `g_strdup`）。而 `中文`、`維護` 是 C 字符串字面量，生命周期与进程相同，不会被释放，所以可以用 `_from_static_string`，省去一次拷贝。

**练习 2**：假设一次按键同时产生了 commit 和新的 context（比如上屏一个标点后，光标后还有未完成的拼音）。代码里 commit 段和 context 段谁先执行？为什么这个顺序是对的？

**参考答案**：commit 段（L296-L303）先执行，context 段（L307 起）后执行。这个顺序是对的：先把确定的文字上屏（让应用文档更新），再画新的预编辑/候选窗。如果反过来，用户会先看到旧的预编辑被新的预编辑替换、然后字才「滞后」上屏，视觉上会出现抖动。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「**给 update 加一次性诊断日志**」的源码阅读型小工程。目标是在不破坏功能的前提下，亲眼看到「一次按键 → 三段式拉取 → 去重/上屏/渲染」的完整链路。

**任务**：

1. 在 [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) 的 `ibus_rime_engine_update` 开头加一条日志，标记「进入了 update」（**示例代码**）：
   ```c
   g_message("[rime-update] ---- update begin ----");
   ```
2. 在三段里各加一条日志：status 段记录「get_status 成功否、是否走了去重 return」；commit 段记录「有无上屏文本」；context 段记录「composition.length」。
   - 提示：判断「是否走了去重 return」可以在 `ibus_rime_update_status` 的 `return;`（L239）前加一条日志。
3. 重新 `make` 编译，运行 ibus-rime，用 `journalctl` 或终端观察日志，做下面这组实验并记录每次「哪几条日志亮了」：
   - **实验 A**：刚获得焦点（触发 `focus_in`），还没打字。
   - **实验 B**：连续敲拼音 `nihao` 但不选词（5 次按键）。
   - **实验 C**：选一个候选词上屏。
   - **实验 D**：点状态栏「中↔A」切到英文，再敲一个字母。

**预期分析**（把你观察到的与下面对照）：

- **A**：update 进入一次；status 段**不走去重**（首次 schema_id 为 NULL，必定刷新）；commit 无；context 段因 `composition.length == 0` 走早退隐藏分支。
- **B**：update 进入 5 次；status 段在第 1 次之后基本都**走了去重 return**（模式没变）；commit 无；context 段每次都正常渲染预编辑/候选（这部分由 u4-l2、u4-l3 详解）。
- **C**：update 进入；status 可能仍去重；commit 有上屏文本；context 段可能因提交后 composition 清空而走早退。
- **D**：update 进入；status 段**不走去重**（`is_ascii_mode` 变了，切了图标）；英文模式下后续按键 commit 会直接上屏 ASCII 字符。

> 这个实践把「投影」模式彻底具象化：无论入口是焦点、按键还是按钮，出口都是同一次三段式 update，而「去重」让其中代价最高的状态栏刷新只在真正变化时发生。

## 6. 本讲小结

- `ibus_rime_engine_update` 是所有外部事件（按键、焦点、按钮、翻页）的**唯一出口**，采用**三段式**结构：`get_status` → `get_commit` → `get_context`，每段都配 `free_xxx` 释放。
- `ibus_rime_update_status` 在开头做**去重**：当 `status` 非空且 `is_disabled`、`is_ascii_mode`、`schema_id` 三者都未变时直接 `return`，避免无谓的状态栏刷新与 D-Bus 往返。
- 状态栏图标走**三态**分支：禁用 → `disabled.png` + `維護`；英文 → `abc.png` + `Abc`；中文 → `zh.png` + 方案名（或回退 `中文`），并从 label 首字符派生顶部指示器的 `symbol`。
- 提交文本 `get_commit` + `ibus_engine_commit_text` 负责真正「上屏」，所有权在调用后转移给 IBus；它必须在 context 渲染之前处理。
- context 段有一个**早退分支**：取不到上下文或 `composition.length == 0` 时隐藏全部 UI 并返回。
- 「真相之源」始终是 librime；前端只做拉取、去重、翻译、下发——这就是「薄前端」在 UI 同步上的具体体现。

## 7. 下一步学习建议

本讲把 `ibus_rime_engine_update` 的**框架**讲完了，但 context 段里最复杂的两块——预编辑文本与候选表——还完全没展开。建议接着学：

- **u4-l2 预编辑与辅助文本渲染**：深入 context 段的 `inline_text` / `auxiliary_text` 构造，理解 `preedit_style`（preview/composition）与 `cursor_type`（insert/select）两种模式如何影响内联预编辑，以及 `color_scheme` 如何给高亮区间上色。
- **u4-l3 候选词列表渲染**：深入 `ibus_lookup_table_clear` 之后那段最长的循环，理解候选词与注释拼接、三类 label 优先级、翻页占位与 cursor_pos 计算。
- 如果你更关心「配置是怎么进来的」，可以跳到 **u5-l1 ibus_rime.yaml 与运行时配置加载**，看本讲反复用到的 `g_ibus_rime_settings` 是如何从 YAML 填充的。
- 如果你想从架构高度回看本讲，**u6-l2 架构取舍与二次开发**会把「投影/薄前端」这套模式总结成可复用的设计原则。
