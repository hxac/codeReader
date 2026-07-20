# 预编辑与辅助文本渲染

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「内联预编辑（inline preedit）」和「辅助文本（auxiliary text）」在 IBus 里是两个不同的 UI 出口，以及它们各自承担什么职责。
- 理解 `embed_preedit` 总开关、`preedit_style`（`preview` / `composition`）与 `cursor_type`（`insert` / `select`）三组配置如何共同决定光标位置和文本来源。
- 读懂 `rime_engine.c` 中 `ibus_rime_engine_update` 的预编辑渲染分支，并能复述两个 `preedit_style` 分支在「文本来源、光标位置、辅助文本是否显示」上的差异。
- 理解 `IBusText` 的属性系统（下划线、前景色、背景色）与 `color_scheme` 颜色方案如何给「正在转换的片段」上色。
- 掌握 librime 里字节偏移（`sel_start` / `sel_end`）与 IBus 字符位置之间的换算关系。

本讲承接 [u4-l1 综合更新函数与状态同步](u4-l1-update-and-status.md)：在那里我们走完了 `ibus_rime_engine_update` 的 status 段与 commit 段，本讲专门钻进它的 context 段后半部分——把 `RimeContext` 翻译成屏幕上的预编辑文本和辅助文本。

## 2. 前置知识

在进入源码前，先建立几个直觉概念。

### 2.1 什么是预编辑文本（preedit text）

在输入法里，用户按下的按键并不会立刻进入应用（比如记事本），而是先由输入法接管，组成一段「尚未确认」的文本，这段文本就叫**预编辑文本**。比如打拼音 `nihao` 时，屏幕上先显示 `ni hao`，这段还在变化的文本就是 preedit。等用户选了候选词「你好」并上屏，preedit 才消失。

IBus 提供了两种 preedit 展示方式：

- **内联（inline / embedded）**：把 preedit 直接插进应用的光标位置，看起来就像用户已经在那里打字。
- **弹出窗口（popup）**：preedit 显示在 IBus 自己管理的一个独立小窗口里，不进入应用文本。

`ibus-rime` 用 `embed_preedit` 这个总开关来选择走哪条路。

### 2.2 什么是辅助文本（auxiliary text）

**辅助文本**是 preedit 之外的第二行提示，通常显示在候选词表的上方。在 `ibus-rime` 里，它被用来显示「还没有进入内联预览的那部分编码」，帮助用户看清自己到底输入了什么、还剩多少没被转换。

### 2.3 什么是「正在转换的片段」

打长句时，librime 会把整串编码分成若干段，其中**当前光标所在、正在被选词的那一段**叫做选中片段（highlighted span）。它在 `RimeContext.composition` 里用 `sel_start` 和 `sel_end` 两个偏移标记出来。本讲的配色高亮，就是专门给这个片段加视觉强调的。

### 2.4 字节偏移 vs 字符位置

这是一个容易踩坑的细节：

- librime 的 `sel_start`、`sel_end`、`cursor_pos` 是**字节偏移**（按 UTF-8 字节数算）。
- IBus 的光标位置和属性区间是**字符位置**（按 Unicode 字符数算）。

一个汉字在 UTF-8 里通常占 3 字节。所以把 librime 的偏移喂给 IBus 之前，必须先换算成字符数。源码里反复出现的 `g_utf8_strlen(preedit, sel_start)` 就是干这件事的——它返回 preedit 前 `sel_start` 个字节里有几个字符。换算关系可以记成：

\[
\text{字符数} = \text{g\_utf8\_strlen}(\text{preedit 字节串},\ \text{字节偏移})
\]

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，另有两个文件提供配置与数据结构支撑。

| 文件 | 角色 | 本讲用到什么 |
|---|---|---|
| [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) | 引擎层，渲染核心 | `ibus_rime_engine_update` 的预编辑/辅助分支、`ibus_rime_create_session` 的 `soft_cursor` 联动 |
| [rime_settings.h](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h) | 设置层头文件 | `enum PreeditStyle`、`enum CursorType`、`struct IBusRimeSettings`、颜色宏 |
| [rime_settings.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c) | 设置层实现 | 颜色方案表 `preset_color_schemes`、默认值、yaml 解析 |
| [ibus_rime.yaml](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml) | 运行时配置 | `style` 下的 `inline_preedit` / `preedit_style` / `cursor_type` / `color_scheme` |

阅读建议：先看 4.1 建立总框架，再顺 4.2 → 4.3 → 4.4 走完一条数据流（来源 → 光标 → 辅助），最后 4.5 统一看属性与配色。

## 4. 核心概念与源码讲解

### 4.1 内联预编辑、辅助文本与 embed_preedit 总开关

#### 4.1.1 概念说明

在 `ibus_rime_engine_update` 的 context 段里，拿到 `RimeContext` 后，前端要决定两件事：

1. **inline_text**：要不要把 preedit 内联进应用？内联的话显示什么？
2. **auxiliary_text**：要不要额外显示一行辅助提示？

这两个决定都依赖同一个总开关——`embed_preedit_text`。它来自 `ibus_rime.yaml` 的 `style/inline_preedit`，语义是「是否把 preedit 内联到输入框」。只有它为真，才会进入后面 `preview` / `composition` 两个内联分支；否则 `inline_text` 保持 `NULL`，交给 IBus 弹窗显示。

#### 4.1.2 核心流程

```text
get_context 成功 且 composition.length > 0
        │
        ├─ 声明 inline_text / auxiliary_text / inline_cursor_pos / preedit_offset
        ├─ 算 has_highlighted_span（是否有选中片段）
        │
        ├─ if embed_preedit 且 preedit_style==PREVIEW  且 支持 commit_text_preview：
        │       inline_text ← commit_text_preview
        ├─ else if embed_preedit 且 preedit_style==COMPOSITION：
        │       inline_text ← composition.preedit
        └─ 否则：inline_text 保持 NULL
        │
        ├─ 用 preedit_offset 决定 auxiliary_text
        ├─ 把 inline_text / auxiliary_text 推给 IBus（或 hide）
```

#### 4.1.3 源码精读

先看 context 段的早退分支——拿不到上下文或编码串为空，就把三个 UI 出口全部隐藏。这部分由 [u4-l1](u4-l1-update-and-status.md) 已经讲过，这里只标注位置：

[rime_engine.c:308-315](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L308-L315) —— 拿不到有效 composition 就 hide preedit / auxiliary / lookup table 并 `return`。

接着是四个局部变量的声明，它们是本讲全部计算的中心：

[rime_engine.c:317-320](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L317-L320) —— `inline_text`（内联文本）、`auxiliary_text`（辅助文本）、`inline_cursor_pos`（内联光标字符位置）、`preedit_offset`（辅助文本从第几个字节开始截）。

然后是 `has_highlighted_span`：

[rime_engine.c:322-323](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L322-L323) —— 当 `sel_start < sel_end` 时认为存在「正在转换的片段」，后续配色与辅助文本都依赖它。

配置枚举定义在头文件里，先认下它们的名字，后面分支会用到：

[rime_settings.h:11-19](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L11-L19) —— `PREEDIT_STYLE_COMPOSITION` / `PREEDIT_STYLE_PREVIEW` 与 `CURSOR_TYPE_INSERT` / `CURSOR_TYPE_SELECT` 两组枚举。

`embed_preedit_text` 字段本身定义在设置结构里：

[rime_settings.h:27-33](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L27-L33) —— `struct IBusRimeSettings`，本讲关心前三个字段 `embed_preedit_text` / `preedit_style` / `cursor_type` 与 `color_scheme` 指针。

#### 4.1.4 代码实践

实践目标：在配置层面感受 `embed_preedit` 总开关的作用。

操作步骤：

1. 打开 `~/.config/ibus/rime/ibus_rime.yaml`（不存在就从源码 `ibus_rime.yaml` 拷一份过去再部署）。
2. 把 `style/inline_preedit` 改成 `false`，保存后点击状态栏「部署」按钮。
3. 在任意文本框切到 Rime，打几个拼音。

需要观察的现象：

- 内联预编辑消失，preedit 改为出现在一个独立的小窗口里。
- 因为两个内联分支都被 `embed_preedit_text` 守门，`inline_text` 保持 `NULL`，于是走到 [rime_engine.c:421-423](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L421-L423) 的 `ibus_engine_hide_preedit_text` 分支。

预期结果：内联开关一关，整条「内联渲染」流水线都被短路。

> 运行效果依赖桌面 IBus 环境，若无图形界面可只做源码阅读：把 [rime_engine.c:326](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L326) 与 [rime_engine.c:364](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L364) 两处 `g_ibus_rime_settings.embed_preedit_text &&` 条件在脑中置为 `false`，即可推出 `inline_text` 恒为 `NULL`。这部分**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果不修改任何配置，`embed_preedit_text` 的默认值是什么？从哪里来？

**答案**：默认为 `TRUE`。来自 [rime_settings.c:16-22](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L16-L22) 的 `ibus_rime_settings_default.embed_preedit_text = TRUE`，只有当 yaml 里 `style/inline_preedit` 被显式读到时才会覆盖（见 [rime_settings.c:53-57](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L53-L57)）。

---

### 4.2 preedit_style：preview 与 composition 两种内联文本来源

#### 4.2.1 概念说明

当 `embed_preedit` 打开后，还有第二个选择：内联里到底显示**什么内容**？这就是 `preedit_style` 决定的事。它有两个取值：

- **composition**：内联显示**原始编码串**（librime 的 `composition.preedit`）。比如打 `ni hao shi jie`，内联里就显示 `ni hao shi jie` 这种还在「组合中」的文本。
- **preview**：内联显示**转换后的预览**（librime 的 `commit_text_preview`），即 librime 当前猜你要上屏的字，比如「你好世界」。这让输入过程看起来更接近「直接打汉字」。

两者的视觉差异很大，但在源码里只是两个并列的 `if` 分支。

#### 4.2.2 核心流程

两个分支的输入输出对照（伪代码）：

```text
PREVIEW 分支（需要 commit_text_preview 字段存在）：
    inline_text      ← commit_text_preview
    整段加单下划线    区间 [0, 预览长度]
    光标(INSERT)     ← 预览长度（末尾）
    光标(SELECT)     ← sel_start 对应的字符位置
    若有选中片段：
        preedit_offset ← sel_start          → 会生成 auxiliary
        配色区间        [sel_start 字符位, 预览长度]
    否则：
        preedit_offset ← composition.length → 不生成 auxiliary

COMPOSITION 分支：
    inline_text      ← composition.preedit
    整段加单下划线    区间 [0, preedit 长度]
    光标(INSERT)     ← cursor_pos 对应的字符位置
    光标(SELECT)     ← sel_start 对应的字符位置
    若有选中片段且配了 color_scheme：
        配色区间        [sel_start 字符位, sel_end 字符位]
    preedit_offset   ← composition.length   → 永远不生成 auxiliary
```

注意一个关键差别：`composition` 分支把 `preedit_offset` 恒定设为全长，因此**永远不会**显示辅助文本；`preview` 分支在有选中片段时会把未转换的尾巴放进辅助文本。这一点是后面综合实践的对比重点。

#### 4.2.3 源码精读

PREVIEW 分支：

[rime_engine.c:326-362](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L326-L362) —— 进入条件是 `embed_preedit_text && preedit_style==PREVIEW && RIME_STRUCT_HAS_MEMBER(context, commit_text_preview) && context.commit_text_preview`。`inline_text` 取自 `context.commit_text_preview`，整段加 `IBUS_ATTR_UNDERLINE_SINGLE` 下划线；`preedit_offset` 在「有选中片段」时设为 `sel_start`（决定辅助文本），否则设为 `composition.length`（不显示辅助）。

COMPOSITION 分支：

[rime_engine.c:364-394](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L364-L394) —— 进入条件是 `embed_preedit_text && preedit_style==COMPOSITION`。`inline_text` 取自 `context.composition.preedit`；末尾把 `preedit_offset` 写成 `composition.length`，这等价于「截取起点已经在字符串末尾」，因此辅助文本必然为空。

两个分支共用同一种「整段单下划线」的写法，只是区间长度不同：

```c
ibus_attr_list_append(
    inline_text->attrs,
    ibus_attr_underline_new(IBUS_ATTR_UNDERLINE_SINGLE, 0, inline_text_len));
```

这正是 [rime_engine.c:337-341](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L337-L341)（PREVIEW）与 [rime_engine.c:374-378](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L374-L378)（COMPOSITION）的两段同构代码。

yaml 里的写法：

[ibus_rime.yaml:12-16](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml#L12-L16) —— 注释说明 `composition` 显示「正在转换的输入」、`preview` 显示「转换后的文本」，默认值在源码仓库里设为 `preview`。解析逻辑在 [rime_settings.c:59-67](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L59-L67)，把字符串 `"composition"` / `"preview"` 映射到枚举。

> 小提醒：`PREVIEW` 分支额外依赖 `RIME_STRUCT_HAS_MEMBER(context, context.commit_text_preview)`——这是 librime 跨版本兼容宏，老版本 librime 没有 `commit_text_preview` 字段时该分支整体跳过，回退到 `COMPOSITION` 行为。

#### 4.2.4 代码实践

实践目标：亲手切换两种 `preedit_style`，对比内联文本内容的变化。

操作步骤：

1. 复制 `ibus_rime.yaml` 到 `~/.config/ibus/rime/`，确保 `inline_preedit: true`。
2. 第一次设 `preedit_style: composition`，部署，在文本框打 `nihao`，观察内联里显示的是不是 `nihao` 之类的编码。
3. 第二次改成 `preedit_style: preview`，部署，再打 `nihao`，观察内联里是不是出现「你好」之类的预览字。
4. （可选）在 [rime_engine.c:330](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L330) 和 [rime_engine.c:366](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L366) 各加一行 `g_debug("inline from: %s", inline_text->text);`，重新编译后用 `G_MESSAGES_DEBUG=all ibus-engine-rime --ibus` 观察日志，确认走的是哪个分支。

需要观察的现象：`composition` 模式内联里是「编码」，`preview` 模式内联里是「汉字预览」。

预期结果：分支选择完全由 `g_ibus_rime_settings.preedit_style` 决定，与按键无关。

> 运行时部署/调试依赖本地 IBus 环境，**待本地验证**；源码阅读部分可直接对照两个分支的 `inline_text` 赋值行确认。

#### 4.2.5 小练习与答案

**练习 1**：仓库自带的 `ibus_rime.yaml` 把 `preedit_style` 设成了什么？为什么强调 `PREVIEW` 还需要 `commit_text_preview` 字段存在？

**答案**：设为 `preview`（[ibus_rime.yaml:16](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml#L16)）。因为 `preview` 模式的内联文本来自 `context.commit_text_preview`，而该字段是 librime 较新版本才加入的，必须用 `RIME_STRUCT_HAS_MEMBER` 探测，老版本上没有这一字段就只能回退。

**练习 2**：为什么 `COMPOSITION` 分支末尾要把 `preedit_offset` 设成 `composition.length`？

**答案**：因为辅助文本的生成条件是 `preedit_offset < composition.length`（见 4.4）。把起点设到字符串末尾，条件为假，就不会生成辅助文本——composition 模式下整串编码都已经内联显示了，没有「未转换尾巴」可提示。

---

### 4.3 cursor_type：insert 与 select 对光标位置的影响

#### 4.3.1 概念说明

`cursor_type` 决定**内联光标停在哪儿**。它有两个取值：

- **insert**：光标停在「插入点」——也就是用户当前正在输入的位置，符合一般打字习惯。
- **select**：光标停在「当前选中片段的开头」——也就是 librime 正在重点转换的那一段的起点。

直觉上：`insert` 像普通编辑器里闪烁的竖线，跟手；`select` 则把光标固定在正在选词的片段头上，方便用户看清「现在轮到哪一段了」。

#### 4.3.2 核心流程

两个 `preedit_style` 分支里，光标位置的计算都是同一个三元表达式，只是 INSERT 分支的取值不同：

```text
inline_cursor_pos = (cursor_type == SELECT)
                  ? g_utf8_strlen(preedit, sel_start)      // 选中片段起点
                  : <INSERT 取值>                           // 见下
```

INSERT 取值随 `preedit_style` 变化：

| preedit_style | INSERT 时的 inline_cursor_pos |
|---|---|
| PREVIEW | `inline_text_len`（预览末尾，因为预览里没有「光标」概念，只能放末尾） |
| COMPOSITION | `g_utf8_strlen(preedit, cursor_pos)`（编码串里真实的插入点） |

SELECT 在两种 style 下都是 `g_utf8_strlen(preedit, sel_start)`。

#### 4.3.3 源码精读

PREVIEW 分支的光标计算：

[rime_engine.c:332-336](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L332-L336) —— `SELECT` 取 `sel_start` 的字符位置，`INSERT` 取预览全长 `inline_text_len`。

COMPOSITION 分支的光标计算：

[rime_engine.c:368-373](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L368-L373) —— `SELECT` 同样取 `sel_start`，`INSERT` 改取 `cursor_pos` 的字符位置。

cursor_type 的配置解析：

[rime_settings.c:69-77](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L69-L77) —— 把 yaml 里的 `"insert"` / `"select"` 映射到枚举。

`cursor_type` 还会反向影响 **librime 自己**的渲染——这是 `ibus_rime_create_session` 里一个容易被忽略的联动：

[rime_engine.c:89-98](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L89-L98) —— 当 `embed_preedit && preedit_style==COMPOSITION && cursor_type==INSERT` 三个条件**同时**成立时，关闭 librime 的 `soft_cursor`（设为 `False`）。理由是：此时真实光标已经由前端内联显示在插入点了，librime 就不必再在编码串里插一个软光标标记（通常是 `‸` 之类），否则会重复。其余情况下 `soft_cursor` 保持开启，让 librime 用软光标提示插入点位置。

#### 4.3.4 代码实践

实践目标：用一个具体例子验证字节偏移到字符位置的换算。

操作步骤：

1. 假设某时刻 `context.composition.preedit` = `"你好世界"`（UTF-8 下每个汉字 3 字节，共 12 字节），`sel_start` = 6（即在第 6 字节处，刚好是「你好」之后）。
2. 手算 `g_utf8_strlen("你好世界", 6)` 的值。
3. 对照 [rime_engine.c:334-335](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L334-L335) 的 SELECT 分支，确认 `inline_cursor_pos` 应该是多少。

需要观察的现象：6 字节 = 2 个汉字，所以结果是 2。

预期结果：`inline_cursor_pos = 2`，即内联光标停在「你好」之后、第 3 个字符「世」之前。换算公式：

\[
\text{inline\_cursor\_pos} = \left\lfloor \frac{\text{sel\_start}}{\text{每字符字节数}} \right\rfloor
\]

对 3 字节汉字即 \(\text{sel\_start} / 3\)。

#### 4.3.5 小练习与答案

**练习 1**：`cursor_type == INSERT` 时，PREVIEW 与 COMPOSITION 两种 style 的光标位置有何本质区别？为什么？

**答案**：PREVIEW 把光标放在**预览全长**（`inline_text_len`），因为预览是已经转换好的汉字串，里面没有「插入点」语义，只能放末尾；COMPOSITION 把光标放在编码串里**真实的 `cursor_pos`**，因为编码串里确实有一个明确的插入位置。

**练习 2**：什么配置组合下 `soft_cursor` 会被关闭？

**答案**：`inline_preedit: true` + `preedit_style: composition` + `cursor_type: insert` 三者同时为真时（[rime_engine.c:93-97](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L93-L97)），`inline_caret` 为真，`soft_cursor` 被设为 `!inline_caret = False`。

---

### 4.4 auxiliary_text：把未转换片段单独显示并高亮

#### 4.4.1 概念说明

辅助文本是 preedit 之外的「第二行」。`ibus-rime` 用它来显示**还没有进入内联预览的那部分编码**。这件事几乎只在 `PREVIEW` 模式下才有意义——因为预览显示的是「转换后的汉字」，用户看不到自己原始输入的尾巴，于是把尾巴放进辅助文本提示。`COMPOSITION` 模式下整串编码都已经内联，没有尾巴可提示，所以辅助文本恒为空。

#### 4.4.2 核心流程

辅助文本的生成完全由 `preedit_offset` 驱动：

```text
if preedit_offset < composition.length：
    preedit_substring ← preedit + preedit_offset     // 从字节偏移截取到末尾
    auxiliary_text    ← 该子串
    若 has_highlighted_span：
        给 auxiliary 在 [sel_start-preedit_offset, sel_end-preedit_offset] 区间
        加 BLACK 前景 / LIGHT 背景
```

回顾 `preedit_offset` 的来源（见 4.2）：

- PREVIEW + 有选中片段：`preedit_offset = sel_start` → 截取选中片段之后的尾巴。
- PREVIEW + 无选中片段：`preedit_offset = composition.length` → 不生成。
- COMPOSITION：`preedit_offset = composition.length` → 不生成。

所以辅助文本**只在「PREVIEW + 有选中片段」时出现**。

#### 4.4.3 源码精读

[rime_engine.c:397-416](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L397-L416) —— 先比较 `preedit_offset < composition.length`，满足才截子串 `context.composition.preedit + preedit_offset` 建 `auxiliary_text`；若有选中片段，再给它套一段固定颜色（`RIME_COLOR_BLACK` 前景 + `RIME_COLOR_LIGHT` 背景）。

注意：辅助文本的高亮用的是**写死的灰色对比**，不是 `color_scheme`：

```c
ibus_attr_foreground_new(RIME_COLOR_BLACK, start, end);
ibus_attr_background_new(RIME_COLOR_LIGHT,  start, end);
```

这两个颜色宏定义在头文件：

[rime_settings.h:7-9](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L7-L9) —— `RIME_COLOR_LIGHT = 0xd4d4d4`（浅灰）、`RIME_COLOR_DARK = 0x606060`、`RIME_COLOR_BLACK = 0x000000`。

最后把两个文本推给 IBus（任一为空就 hide 对应出口）：

[rime_engine.c:418-429](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L418-L429) —— `inline_text` 非空调 `ibus_engine_update_preedit_text`，否则 hide；`auxiliary_text` 非空调 `ibus_engine_update_auxiliary_text`，否则 hide。

#### 4.4.4 代码实践

实践目标：构造一个能稳定看到辅助文本的场景。

操作步骤：

1. 配置 `inline_preedit: true` + `preedit_style: preview`（仓库默认即如此）。
2. 选一个支持长句的方案（如「朙月拼音」），连续打一长串拼音（例如 `wohenxihezuotianquchaoishi` 之类），让 librime 把句子分多段转换。
3. 观察候选表上方是否出现一行额外的编码提示（即 auxiliary_text）。

需要观察的现象：当光标停在中间某段时，预览显示前半段汉字，辅助行显示**尚未转换的后半段编码**；这段辅助行里「当前选中片段」会被浅灰底色高亮。

预期结果：只有在 `PREVIEW` 且存在 `sel_start < sel_end` 时，辅助行才会出现，与 [rime_engine.c:397](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L397) 的判断一致。

> 该现象依赖桌面与输入方案，**待本地验证**；源码层面可由「`preedit_offset` 仅在 PREVIEW+highlight 时小于 length」推出。

#### 4.4.5 小练习与答案

**练习 1**：把 `preedit_style` 从 `preview` 改成 `composition`，辅助文本会怎样？

**答案**：消失。因为 COMPOSITION 分支把 `preedit_offset` 设为 `composition.length`，[rime_engine.c:397](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L397) 的 `preedit_offset < context.composition.length` 为假，直接跳过辅助文本生成。

**练习 2**：辅助文本的高亮颜色受 `color_scheme` 影响吗？

**答案**：不受。辅助文本恒用 `RIME_COLOR_BLACK` / `RIME_COLOR_LIGHT`（[rime_engine.c:411-414](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L411-L414)），`color_scheme` 只影响 `inline_text` 的配色（见 4.5）。

---

### 4.5 IBusText 属性系统：下划线与 color_scheme 颜色高亮

#### 4.5.1 概念说明

`IBusText` 不只是一个字符串，它还能携带一组**属性（attribute）**，每条属性描述「在某个字符区间内套什么样式」。`ibus-rime` 用到的属性有三种：

- **下划线**（`IBUS_ATTR_UNDERLINE_SINGLE`）：整段 preedit 加单下划线，告诉应用「这段是还没确认的预编辑」。
- **前景色**（`ibus_attr_foreground_new`）：改文字颜色。
- **背景色**（`ibus_attr_background_new`）：改文字底色。

「正在转换的片段」就是靠前景+背景色一起高亮的。高亮用哪套颜色由 `color_scheme` 决定——它把 yaml 里的一个名字（如 `aqua`）映射到一对 `(text_color, back_color)`。

#### 4.5.2 核心流程

```text
1. inline_text->attrs = ibus_attr_list_new();          // 建空属性表
2. 整段加下划线：underline_single, [0, inline_text_len]
3. 若 has_highlighted_span 且配了 color_scheme：
     PREVIEW:    加 fg(text_color) + bg(back_color) 于 [sel_start 字符位, 预览长度]
     COMPOSITION:加 fg(text_color) + bg(back_color) 于 [sel_start 字符位, sel_end 字符位]
```

属性区间端点都是**字符位置**（不是字节），所以同样要经过 `g_utf8_strlen` 换算。

#### 4.5.3 源码精读

PREVIEW 配色块：

[rime_engine.c:343-357](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L343-L357) —— `start` 是 `sel_start` 的字符位置，`end` 是预览全长，配色取自 `g_ibus_rime_settings.color_scheme->text_color` 与 `back_color`。

COMPOSITION 配色块：

[rime_engine.c:379-392](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L379-L392) —— `start`/`end` 分别是 `sel_start`/`sel_end` 的字符位置，颜色同样取自 `color_scheme`。

颜色方案表与查找：

[rime_settings.c:8-14](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L8-L14) —— 内置四种方案 `aqua` / `azure` / `ink` / `luna`，每项给 `text_color` 与 `back_color` 一对 RGB 值。

| 方案 | text_color（前景） | back_color（背景） |
|---|---|---|
| aqua | `0xffffff`（白） | `0x0a3dfa`（蓝） |
| azure | `0xffffff`（白） | `0x0a3dea`（蓝） |
| ink | `0xffffff`（白） | `0x000000`（黑） |
| luna | `0x000000`（黑） | `0xffff7f`（浅黄） |

[rime_settings.c:26-40](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L26-L40) —— `select_color_scheme` 按名字在表里线性查找，找到就把 `settings->color_scheme` 指向那一项，找不到则置 `NULL`（回落到「不高亮」）。

[rime_settings.c:85-89](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L85-L89) —— 读 yaml 的 `style/color_scheme`，有值才调用 `select_color_scheme`。

[ibus_rime.yaml:24-28](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml#L24-L28) —— yaml 里 `color_scheme: ~` 表示默认不启用高亮（`~` 是 YAML 的 null）。

因此整条链路是：

```text
ibus_rime.yaml: style/color_scheme: aqua
   → config_get_cstring 读到 "aqua"
   → select_color_scheme 在 preset_color_schemes 命中
   → g_ibus_rime_settings.color_scheme 指向 {aqua, 0xffffff, 0x0a3dfa}
   → 渲染时 ibus_attr_foreground_new(0xffffff, ...) / ibus_attr_background_new(0x0a3dfa, ...)
```

#### 4.5.4 代码实践

实践目标：验证 `color_scheme` 对内联高亮的影响。

操作步骤：

1. 保持 `inline_preedit: true` + `preedit_style: preview`。
2. 把 `color_scheme: ~` 改成 `color_scheme: aqua`，部署。
3. 打一段会被分段转换的长拼音，观察内联预览里「选中片段」是否变成蓝底白字。
4. 再改成 `luna`，观察是否变成浅黄底黑字。

需要观察的现象：选中片段的颜色随方案变化；设回 `~` 则无高亮。

预期结果：颜色与 [rime_settings.c:8-14](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L8-L14) 表完全对应。

> 颜色渲染依赖桌面 IBus + 应用对 preedit 属性的支持，**待本地验证**；部分应用可能忽略前景/背景属性，仅保留下划线。

#### 4.5.5 小练习与答案

**练习 1**：为什么 PREVIEW 配色区间的 `end` 是「预览全长」而不是 `sel_end`？

**答案**：因为 PREVIEW 的 `inline_text` 是转换后的预览（`commit_text_preview`），它的长度和字节布局与原始 `composition.preedit` 不同，`sel_end` 这个字节偏移无法直接映射到预览串上；代码选择「从选中片段起点一直高亮到预览末尾」，用 `inline_text_len` 作 `end` 是一种可实现的近似（见 [rime_engine.c:343-348](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L343-L348)）。

**练习 2**：如果用户在 yaml 里写了一个不存在的方案名（比如 `color_scheme: pink`），会发生什么？

**答案**：`select_color_scheme` 遍历 `preset_color_schemes` 找不到 `pink`，把 `color_scheme` 置为 `NULL`（[rime_settings.c:38-39](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L38-L39)），于是渲染分支里 `if (... && g_ibus_rime_settings.color_scheme)` 为假，不套任何颜色，等同于不高亮。

---

## 5. 综合实践

把本讲所有知识点串起来，完成下面这张「两分支差异表」，并用配置验证。

### 5.1 任务一：填写差异对照表

对照源码 [rime_engine.c:326-394](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L326-L394)，填写并核对下表（答案见 5.3）：

| 维度 | PREEDIT_STYLE_PREVIEW | PREEDIT_STYLE_COMPOSITION |
|---|---|---|
| `inline_text` 来源 | ？ | ？ |
| 整段下划线区间 | ？ | ？ |
| 光标位置（INSERT） | ？ | ？ |
| 光标位置（SELECT） | ？ | ？ |
| color_scheme 高亮区间 | ？ | ？ |
| `preedit_offset` 取值 | ？ | ？ |
| 是否会产生 auxiliary_text | ？ | ？ |
| 是否需要 `commit_text_preview` 字段 | ？ | ？ |

### 5.2 任务二：配置走查

完成下面四种组合的配置走查（纯源码阅读，不必真跑）：

| 组合 | inline_preedit | preedit_style | cursor_type | 预期：内联显示什么？光标在哪？有无辅助行？soft_cursor 开关？ |
|---|---|---|---|---|
| A | true | preview | select | ？ |
| B | true | composition | insert | ？ |
| C | true | composition | select | ？ |
| D | false | preview | insert | ？ |

对每种组合，逐条写出理由并指向具体源码行。

### 5.3 参考答案

**任务一对照表**：

| 维度 | PREVIEW | COMPOSITION |
|---|---|---|
| `inline_text` 来源 | `commit_text_preview` | `composition.preedit` |
| 整段下划线区间 | `[0, 预览长度]` | `[0, preedit 长度]` |
| 光标（INSERT） | 预览长度（末尾） | `g_utf8_strlen(preedit, cursor_pos)` |
| 光标（SELECT） | `g_utf8_strlen(preedit, sel_start)` | `g_utf8_strlen(preedit, sel_start)` |
| color_scheme 高亮 | `[sel_start 字符位, 预览长度]` | `[sel_start 字符位, sel_end 字符位]` |
| `preedit_offset` | `sel_start`（有高亮）/ `length`（无） | `length`（恒定） |
| 是否产生 auxiliary | 有高亮时**会** | **不会** |
| 需要 `commit_text_preview` | **是**（带版本探测） | 否 |

**任务二走查**：

- **A**（preview + select）：内联显示汉字预览；光标在选中片段起点；有选中片段时辅助行显示未转换尾巴；`soft_cursor` 开启（非 composition+insert 组合）。
- **B**（composition + insert）：内联显示原始编码；光标在真实插入点 `cursor_pos`；无辅助行；`soft_cursor` **关闭**（三条件全中，见 [rime_engine.c:93-97](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L93-L97)）。
- **C**（composition + select）：内联显示原始编码；光标在选中片段起点；无辅助行；`soft_cursor` 开启。
- **D**（embed 关闭）：两个内联分支都被守门跳过，`inline_text=NULL`，走 [rime_engine.c:421-423](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L421-L423) 的 hide 分支，preedit 改由 IBus 弹窗显示。

## 6. 本讲小结

- `ibus_rime_engine_update` 的 context 段把 `RimeContext` 翻译成两类 UI 文本：**内联预编辑** `inline_text` 和**辅助文本** `auxiliary_text`。
- `embed_preedit` 是总开关，只有为真才进入 `preview` / `composition` 两个内联分支；为假则 preedit 交给 IBus 弹窗。
- `preedit_style` 决定内联文本来源：`preview` 取 `commit_text_preview`（转换后的汉字），`composition` 取 `composition.preedit`（原始编码）。
- `cursor_type` 决定内联光标位置：`insert` 跟随插入点，`select` 钉在选中片段起点；它还会反向控制 librime 的 `soft_cursor` 开关。
- 辅助文本只在 **PREVIEW + 有选中片段** 时出现，显示尚未进入预览的编码尾巴，高亮用固定灰（`RIME_COLOR_BLACK`/`LIGHT`）。
- `IBusText` 属性系统提供下划线 + 前景/背景色；`color_scheme`（aqua/azure/ink/luna）只影响 `inline_text` 的选中片段配色，且可通过 `~` 关闭。
- 字节偏移（`sel_start`/`sel_end`/`cursor_pos`）必须经 `g_utf8_strlen` 换算成字符位置后才能喂给 IBus。

## 7. 下一步学习建议

- 继续阅读 [u4-l3 候选词列表渲染](u4-l3-candidate-table.md)，看 `ibus_rime_engine_update` 的最后一段如何把 `context.menu` 翻译成 `IBusLookupTable`，注意那里同样大量用到「字符位置」与「属性着色」（候选词注释用 `RIME_COLOR_DARK`）。
- 想理解这些 `style/*` 配置是怎么从 yaml 一路流到全局 `g_ibus_rime_settings` 的，回到 [u5-l1 ibus_rime.yaml 与运行时配置加载](u5-l1-yaml-config-loading.md)。
- 若想动手扩展（比如新增一个 `preedit_style` 取值或一种 color_scheme），参考 [u6-l2 架构取舍与二次开发](u6-l2-architecture-and-extension.md) 的改造路径。
