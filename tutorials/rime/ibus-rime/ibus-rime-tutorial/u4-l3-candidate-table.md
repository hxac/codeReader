# 候选词列表渲染

## 1. 本讲目标

本讲是「前端 UI 渲染」专题（U4）的最后一篇，聚焦 `ibus_rime_engine_update` 中最复杂的一段：把 librime 给出的候选菜单 `RimeContext.menu` 翻译成 IBus 能显示的 `IBusLookupTable`。

学完后你应该能够：

- 说清 `IBusLookupTable` 这个对象在 ibus-rime 里的创建、清空、追加候选、设置标签、提交的完整流程。
- 解释候选词与其注释（comment）是如何拼成一行、并把注释部分着成灰色的。
- 默写出候选序号 label 的三级优先级：`select_labels` → `select_keys` → 数字。
- 理解「最后一页插入 `page_size` 个空候选」这件事的目的，以及它对 `cursor_pos` 计算的影响。
- 看懂鼠标点选 `candidate_clicked` 如何把「页内下标」换算成「全局候选下标」，以及翻页回调如何与 librime 联动。

本讲承接 [u4-l2](u4-l2-preedit-and-auxiliary.md)：那里讲的是 context 段里的「内联预编辑 + 辅助文本」，本讲讲的是同一段里紧随其后的「候选表」出口。

## 2. 前置知识

阅读本讲前，你需要先建立以下几个直觉（若不熟悉，建议先看 U4 前两讲与 U3）：

- **薄前端 / 投影模型**：ibus-rime 自己不查词、不分页。librime 算好「当前这一页有哪些候选、高亮了第几个、是不是最后一页」，ibus-rime 只把这些状态**投影**成 IBus 的 UI 原语。候选表的所有数据都来自 `rime_api->get_context()` 返回的 `RimeContext.menu`。
- **`RimeContext.menu` 的关键字段**（来自 librime）：
  - `candidates[i].text` / `candidates[i].comment`：第 i 个候选的正文与注释（如拼音、释义）。
  - `num_candidates`：本页候选数。
  - `page_size`：每页容量（如 5）。
  - `page_no`：当前页号，从 0 开始。
  - `is_last_page`：是否最后一页。
  - `highlighted_candidate_index`：本页内高亮候选的下标。
  - `select_keys`：方案定义的「选择键」字符串（如 `"12345"`），可空。
- **`context.select_labels`**：方案自定义的候选标签数组（如 `1. 2. 3.` 或 `甲 乙 丙`），可空。注意它挂在 `context` 上而非 `menu` 上。
- **`IBusText` 的属性系统**：一个 `IBusText` 不只是字符串，还带一个属性链表 `attrs`，可以给某段区间加下划线、前景色、背景色。u4-l2 已详细讲过，本讲会再用它给注释着色。
- **`IBusLookupTable`**：IBus 的候选表对象。它内部维护一串候选 `IBusText`、一串标签 `IBusText`、一个 `page_size`、一个 `cursor_pos`、一个 `round`（翻页是否循环）标志和一个方向（横/竖）。

一个贯穿全讲的关键认知：**ibus-rime 每次 update 只把「当前这一页」的候选喂给 IBus**。IBus 并不知道 librime 还藏着前后几页。后面会看到，正是这一点逼出了「插入空占位候选」这套看似奇怪的逻辑。

## 3. 本讲源码地图

本讲几乎全部内容集中在 `rime_engine.c`，设置层只提供「方向」这一个输入：

| 文件 | 作用 |
| --- | --- |
| `rime_engine.c` | 候选表的创建、构建、提交、鼠标点选、翻页回调全在这里。 |
| `rime_settings.h` | 定义 `IBusRimeSettings`，其中 `lookup_table_orientation` 决定候选表横排/竖排；定义颜色常量 `RIME_COLOR_DARK`。 |
| `rime_settings.c` | 从 `ibus_rime.yaml` 的 `style/horizontal` 读出方向，写入 `lookup_table_orientation`。 |

涉及的源码点（均在当前 HEAD `ba8bfc3`）：

- 候选表对象的创建：`rime_engine.c` 的 `ibus_rime_engine_init`。
- 候选表构建主循环：`ibus_rime_engine_update` 的 context 段尾部。
- 鼠标点选：`ibus_rime_engine_candidate_clicked`。
- 翻页：`ibus_rime_engine_page_up` / `ibus_rime_engine_page_down`。
- 方向输入：`rime_settings.c` 的 `ibus_rime_load_settings` 中 `style/horizontal` 分支。

## 4. 核心概念与源码讲解

### 4.1 IBusLookupTable 候选表对象：创建、清空与提交

#### 4.1.1 概念说明

`IBusLookupTable` 是 IBus 提供给引擎的「候选词列表」抽象。引擎把候选一个个塞进去，再调用 `ibus_engine_update_lookup_table` 交给 IBus 面板（panel）绘制。它本质上是两个并行数组——候选正文数组和标签数组——加上几个分页参数。

在 ibus-rime 里，这张表不是每次 update 现建现弃的，而是作为引擎实例的成员**长期持有**，每次 update 先清空再重填。这样做的好处是对象只分配一次，避免高频按键下的内存抖动。

#### 4.1.2 核心流程

```
引擎 init：
  ibus_lookup_table_new(9, 0, TRUE, FALSE)  →  建表，默认 page_size=9
  g_object_ref_sink(table)                   →  认领浮动引用

每次 update（有候选时）：
  ibus_lookup_table_clear(table)             →  清空旧候选与标签
  set_round / set_page_size                  →  设分页参数
  append_candidate × N  +  set_label × N     →  逐个填候选与标签
  set_cursor_pos / set_orientation           →  定位光标、设方向
  ibus_engine_update_lookup_table(...)       →  提交给 IBus 面板

每次 update（无候选时）：
  ibus_engine_hide_lookup_table(...)         →  隐藏候选窗口
```

#### 4.1.3 源码精读

引擎实例结构体里，`table` 是四个长期成员之一：

[engine.c 成员 table](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L15-L23) —— `IBusRimeEngine` 持有 `session_id`、`status`、`table`、`props`，注意首成员必须是父类 `IBusEngine` 以保证 GObject 内存布局兼容（详见 u3-l1）。

表的创建发生在 `init`：

[rime_engine.c:108-109](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L108-L109) —— `ibus_lookup_table_new(9, 0, TRUE, FALSE)`。这四个参数依次是 `page_size, cursor_pos, cursor_visible, round`：初始每页 9 项、光标在第 0 个、光标可见、不循环。这里的 `9` 只是个占位默认值，真正生效的每页大小在 update 时由 `set_page_size(context.menu.page_size)` 覆盖。紧接着的 `g_object_ref_sink` 把 GLib 的「浮动引用」转成引擎自己持有的拥有引用（u2-l2 讲过同样模式）。

每次 update 开头先无条件清空：

[rime_engine.c:431](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L431) —— `ibus_lookup_table_clear`。无论本次有没有候选都先清，保证不会把上一帧的残留候选显示出来。

填完后提交给 IBus：

[rime_engine.c:498-499](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L498-L499) —— `ibus_engine_update_lookup_table(engine, table, TRUE)`，第三个参数 `TRUE` 表示「可见」。注意 table 对象本身的所有权还在引擎手里，IBus 只是读取并绘制。

若无候选则隐藏：

[rime_engine.c:501-503](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L501-L503) —— 当 `context.menu.num_candidates == 0` 时直接 `ibus_engine_hide_lookup_table`。

#### 4.1.4 代码实践

**实践目标**：确认候选表对象的生命周期是「一次创建、反复清填」。

**操作步骤**：

1. 在 [rime_engine.c:108](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L108) 的 `ibus_lookup_table_new` 调用前临时加一行 `g_message("table created at init");`。
2. 在 [rime_engine.c:431](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L431) 的 `ibus_lookup_table_clear` 前临时加一行 `g_message("table cleared at update");`。
3. 重新编译并以 `G_MESSAGES_DEBUG=all` 环境变量运行 `ibus-engine-rime --ibus`，连续敲几个拼音。

**需要观察的现象**：「table created」只应出现一次（引擎 init 时），而「table cleared」会在每次按键带来候选变化时反复出现。

**预期结果**：验证了表是长期持有、反复清填的对象，而非每次 update 重建。

> 本环境无法启动 IBus 守护进程，上述运行结果为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ibus_lookup_table_new` 里的 `9` 不需要和方案的真实 `page_size` 一致？

**参考答案**：因为它只是个初始占位值，真正生效的每页大小在 update 中由 `ibus_lookup_table_set_page_size(rime_engine->table, context.menu.page_size)`（[rime_engine.c:445](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L445)）覆盖。候选表对象在 init 时就创建，那时还没读到方案的 `page_size`。

**练习 2**：`ibus_engine_update_lookup_table` 的第三个参数 `TRUE` 表示什么？把它改成 `FALSE` 会出现什么现象？

**参考答案**：表示「提交后立即可见」（visible）。若改成 `FALSE`，IBus 面板会拿到候选数据但不显示候选窗口；通常配合稍后再调 show 使用。ibus-rime 始终用 `TRUE`，即「有候选就立刻显示」。

---

### 4.2 候选词与注释的拼接与着色

#### 4.2.1 概念说明

每个候选在 librime 里是 `text`（候选正文，如「你好」）和可选的 `comment`（注释，如拼音 `ni hao` 或分类标记）。IBus 的候选窗口里，ibus-rime 选择把两者拼在同一行展示：先正文，再一个空格，再注释。同时，为了让正文更醒目、注释退居二线，注释部分（含那个分隔空格）会被着成灰色。

这里的灰色就是设置头里的 `RIME_COLOR_DARK`：

[rime_settings.h:7-9](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L7-L9)（颜色常量定义于 `rime_settings.h`）—— `RIME_COLOR_DARK = 0x606060`（中灰），`RIME_COLOR_LIGHT = 0xd4d4d4`，`RIME_COLOR_BLACK = 0x000000`。注意这些是 RGB 整数，不是 IBus 的封装对象。

#### 4.2.2 核心流程

对第 i 个候选：

```
取 text = candidates[i].text
取 comment = candidates[i].comment   (可能为 NULL)

if comment 非空:
    temp = text + " " + comment            # g_strconcat 三段拼接
    cand_text = IBusText(temp)
    text_len = 字符数(text)                  # g_utf8_strlen，按 Unicode 字符计
    end_index = 字符数(cand_text)            # 整行长度
    给 cand_text 在区间 [text_len, end_index) 加前景色 RIME_COLOR_DARK
else:
    cand_text = IBusText(text)

append_candidate(table, cand_text)
```

关键细节：着色区间是 `[text_len, end_index)`，也就是「空格 + 注释」整段都变灰，正文保持默认颜色。

#### 4.2.3 源码精读

[rime_engine.c:452-470](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L452-L470) —— 这是候选正文构建的核心。逐句说明：

- `gchar* temp = g_strconcat(text, " ", comment, NULL);`：GLib 的字符串拼接，以 `NULL` 结尾的可变参数，产出新串 `"text comment"`，需调用者 `g_free`。
- `cand_text = ibus_text_new_from_string(temp); g_free(temp);`：用拼好的串造 `IBusText`，构造函数会复制内容，所以 `temp` 立刻可释放。
- `int text_len = g_utf8_strlen(text, -1);`：**按字符**算正文长度（不是字节数）。中文一个字是 3 个 UTF-8 字节但算 1 个字符，这里必须用字符数，因为 `IBusText` 的属性区间以字符下标为单位。
- `int end_index = ibus_text_get_length(cand_text);`：整行的字符长度（含空格与注释）。
- `ibus_text_append_attribute(cand_text, IBUS_ATTR_TYPE_FOREGROUND, RIME_COLOR_DARK, text_len, end_index);`：在 `[text_len, end_index)` 这段加前景色。`text_len` 恰好是正文之后第一个字符（即那个空格）的下标，所以空格也被着色。

> 注意：这里用的是 `ibus_text_append_attribute`（往已有 `IBusText` 追加属性），而 u4-l2 里预编辑文本用的是先 `ibus_attr_list_new()` 再 `ibus_attr_list_append`。两种写法等价，区别只在于「表是否已存在」：`append_attribute` 会在 `attrs` 为空时自动建表。

#### 4.2.4 代码实践

**实践目标**：验证着色区间的起点是「正文长度」，即空格也被染灰。

**操作步骤**：

1. 找一个会显示注释的方案（多数拼音方案默认显示）。
2. 阅读 [rime_engine.c:460-465](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L460-L465)，确认 `text_len` 来自 `g_utf8_strlen(text, -1)`（不含空格、不含注释）。
3. 临时把着色起点从 `text_len` 改成 `text_len + 1`（跳过空格），重新编译运行，观察注释前的空格颜色变化。

**需要观察的现象**：原代码下，候选正文（如「你好」）是默认前景色，其后的「 ni hao」（含前导空格）整体呈灰色；改成 `text_len + 1` 后，分隔空格会变回默认色，只有 `ni hao` 是灰色。

**预期结果**：肉眼可分辨空格归属哪段颜色，从而确认区间边界 `[text_len, end_index)` 的含义。

> 运行结果「待本地验证」；本实践为「修改参数并观察」型。

#### 4.2.5 小练习与答案

**练习 1**：为什么计算 `text_len` 必须用 `g_utf8_strlen` 而不能用 C 的 `strlen`？

**参考答案**：`strlen` 返回字节数，而中文 UTF-8 一个字占 3 字节。`IBusText` 的属性区间下标以**字符**为单位，若用字节长度，着色起点会偏到注释中间甚至越界，导致灰色块错位。`g_utf8_strlen(text, -1)` 按 Unicode 字符计数，才是正确的区间坐标。

**练习 2**：若某候选的 `comment` 为 `NULL`，会发生什么？

**参考答案**：走 `else` 分支，`cand_text = ibus_text_new_from_string(text)`，只显示正文、不加任何属性，整行都是默认颜色（见 [rime_engine.c:467-469](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L467-L469)）。

---

### 4.3 候选序号 label 的三级优先级

#### 4.3.1 概念说明

每个候选除了正文，还有一个**标签**（label）显示在正文左侧或上方，用来告诉用户「按哪个键选这个词」。ibus-rime 给标签设计了三级优先级，从高到低：

1. **方案自定义标签 `select_labels`**：方案可以给每页候选指定任意标签（如 `甲 乙 丙 丁 戊` 或 `1. 2. 3.`）。
2. **选择键 `select_keys`**：方案定义的「直接选词键」串（如 `"abcdef"`），按字符取出。
3. **数字回退**：`(i + 1) % 10`，即 `1 2 3 … 9 0`。

这三个来源并非简单「有则用」，它们还各自带一个**下标约束**，组合起来才形成完整规则。

#### 4.3.2 核心流程

```
has_labels = (select_labels 字段存在 且 非空)
num_select_keys = select_keys ? strlen(select_keys) : 0

对第 i 个候选（i 是页内下标）：
  if (i < page_size 且 has_labels):
      label = select_labels[i]              # 第 1 级
  else if (i < num_select_keys):
      label = select_keys 的第 i 个字符      # 第 2 级
  else:
      label = (i + 1) % 10 的数字字符串      # 第 3 级
  set_label(table, i, label)
```

两个关键约束：

- 第 1 级要求 `i < page_size`：自定义标签按「页」设计，只在第一屏（前 `page_size` 个）生效。
- 第 2 级要求 `i < num_select_keys`：选择键串有多长，就只能给前几个候选当标签。

#### 4.3.3 源码精读

先看两个开关的求值：

[rime_engine.c:433-438](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L433-L438) —— `num_select_keys` 用 `strlen`（选择键是 ASCII，字节=字符，没问题）；`has_labels` 用 `RIME_STRUCT_HAS_MEMBER(context, context.select_labels) && context.select_labels` 双重判断，前者防结构体版本不含该字段（跨版本 ABI 保护，u2-l3 讲过 `RIME_STRUCT` 机制），后者防指针为空。

三级判断本体：

[rime_engine.c:471-481](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L471-L481) ——

- 第 1 级 `ibus_text_new_from_string(context.select_labels[i])`：自定义标签是字符串数组。
- 第 2 级 `ibus_text_new_from_unichar(context.menu.select_keys[i])`：选择键是字符串，取第 i 个**字符**（Unicode 码点）。
- 第 3 级 `ibus_text_new_from_printf("%d", (i + 1) % 10)`：注意是 `(i+1) % 10`，所以序号是 `1,2,…,9,0` 循环——第 10 个候选（i=9）显示 `0`。

注意 `set_label(table, i, label)` 用的是**页内下标 `i`**，而不是 `append`。这与候选正文用 `append_candidate` 不同：标签是按下标随机写入的。

> 一个易错点：第 1 级的 `has_labels` 没有判断 `i < num_candidates`，但它要求 `i < page_size`；而本页候选数 `num_candidates ≤ page_size`，所以循环里 `i` 永远不会超过 `num_candidates`，自然不会越界访问 `select_labels`。

#### 4.3.4 代码实践

**实践目标**：在源码上标注三级 label 的优先级判断，并推断不同方案下的显示。

**操作步骤**：

1. 打开 [rime_engine.c:471-481](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L471-L481)，在三个分支旁分别注释 `// 第1级 select_labels`、`// 第2级 select_keys`、`// 第3级 数字`。
2. 假设某方案 `page_size = 5`，`select_keys = "aeiou"`，**没有** `select_labels`，本页 5 个候选。手推每个候选的 label。
3. 再假设同一方案但**有** `select_labels = {"甲","乙","丙","丁","戊"}`，手推 label。

**需要观察的现象（纯推理，不需运行）**：

- 无 `select_labels` 时：i=0..4，`has_labels` 为假，跳过第 1 级；i < `num_select_keys`(=5) 全部成立，走第 2 级，label 为 `a e i o u`。
- 有 `select_labels` 时：i=0..4 且 i < `page_size`(=5) 全部成立，走第 1 级，label 为 `甲 乙 丙 丁 戊`，`select_keys` 完全被压制。

**预期结果**：验证「自定义标签优先于选择键」、以及「下标约束 `i < page_size` 与 `i < num_select_keys`」如何决定每个候选落到哪一级。

#### 4.3.5 小练习与答案

**练习 1**：第 3 级为何用 `(i + 1) % 10` 而不是 `i + 1`？

**参考答案**：候选序号习惯从 1 开始（用户心理模型是「按 1 选第一个」），所以 `+1`；而 `page_size` 可能大于 10（虽然少见），`% 10` 保证序号在 `0..9` 单数字范围内、回到 0，避免出现两位数标签破坏排版。即序号序列是 `1 2 3 4 5 6 7 8 9 0 1 2 …`。

**练习 2**：如果方案的 `select_keys` 只有 3 个字符，但本页有 5 个候选，且没有 `select_labels`，5 个候选的 label 分别是什么？

**参考答案**：i=0,1,2 走第 2 级，取 `select_keys[0..2]`；i=3,4 时 `i < num_select_keys`(=3) 不成立，走第 3 级，得 `(3+1)%10=4`、`(4+1)%10=5`。所以依次是：`select_keys[0], select_keys[1], select_keys[2], 4, 5`。

---

### 4.4 分页占位、round、方向与光标定位

> ⚠️ 这是本讲最绕的一节，也是练习任务的核心。请放慢节奏。

#### 4.4.1 概念说明

前面提过一个关键事实：**ibus-rime 每次 update 只把「当前这一页」的候选喂给 IBus**。从 IBus 面板的视角看，它每次都只收到一小撮候选，并不知道 librime 那边还有「上一页/下一页」。

但用户需要看到翻页提示（上一页/下一页箭头）。为了让 IBus 面板愿意画出这些导航元素，ibus-rime 用了一个技巧：**人为塞入「空字符串占位候选」**，改变 IBus 看到的候选总数与光标位置，从而诱导面板渲染翻页导航。代码里的注释直白地写了意图：`//show page up for last page`、`//show page down except last page`。

由此引出本节的三个难点：

1. **占位候选**：最后一页前置插入 `page_size` 个空候选；非最后一页尾部追加 1 个空候选。
2. **`round` 标志**：只在「中间页」开启，首尾页关闭。
3. **`cursor_pos` 偏移**：因为前置占位挪动了真实候选的位置，光标必须相应偏移才能落回高亮项。

此外还有「方向」（横排/竖排），它来自配置，相对简单。

#### 4.4.2 核心流程

先看四个布尔/参数的求值（[rime_engine.c:439-445](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L439-L445)）：

```
has_page_down = !is_last_page                         # 非最后页 → 还有下一页
has_page_up   = is_last_page && page_no > 0           # 最后页且非首页 → 还有上一页
round         = !(is_last_page || page_no == 0)       # 既非最后页也非首页（即中间页）才为真
page_size     = context.menu.page_size
```

填候选的顺序（三段）：

```
阶段 A：若 has_page_up，先 append  page_size 个空候选   # 前置占位
阶段 B：for i in 0..num_candidates: append 真实候选      # 本体
阶段 C：若 has_page_down，再 append 1 个空候选           # 尾部占位
```

设置光标：

```
if has_page_up:
    cursor_pos = page_size + highlighted_candidate_index   # 补偿前置占位
else:
    cursor_pos = highlighted_candidate_index               # 无前置，直接用
```

设置方向并提交：

```
set_orientation(table, lookup_table_orientation)
update_lookup_table(engine, table, TRUE)
```

#### 4.4.3 源码精读

**四个标志与 round**：

[rime_engine.c:439-445](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L439-L445) —— 重点看 `round` 的逻辑：

\[ \text{round} = \neg(\text{is\_last\_page} \lor (\text{page\_no}=0)) \]

即 `round` 仅在「中间页」（既不是第 0 页、也不是最后一页）为真。`round` 是 IBus 的「翻页是否循环」标志：为真时，在最后一页再向下翻会回到第一页。ibus-rime 选择只在中间页开启它——首尾页用占位候选处理导航，避免循环带来的歧义。

**前置占位（最后一页）**：

[rime_engine.c:446-451](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L446-L451) —— 在写入真实候选**之前**，先塞入 `page_size` 个空串候选。这就是注释说的 "show page up for last page"：制造一个「幽灵上一页」，让 IBus 面板认为当前页之前还有内容，从而画出「上一页」箭头。

**尾部占位（非最后一页）**：

[rime_engine.c:483-486](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L483-L486) —— 在真实候选**之后**追加 1 个空串候选，对应 "show page down except last page"：让面板看到「还有多余候选」，画出「下一页」箭头。

**光标定位（最关键的偏移）**：

[rime_engine.c:487-495](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L487-L495) ——

```c
if (has_page_up) {
  ibus_lookup_table_set_cursor_pos(
      rime_engine->table,
      context.menu.page_size + context.menu.highlighted_candidate_index);
} else {
  ibus_lookup_table_set_cursor_pos(
      rime_engine->table, context.menu.highlighted_candidate_index);
}
```

为什么最后一页要加 `page_size`？因为阶段 A 已经在真实候选前面插入了 `page_size` 个空候选，整个表的下标被整体平移了 `page_size`：

\[ \text{真实候选 } i \text{ 的绝对下标} = \text{page\_size} + i \]

 librime 报告的高亮项是页内下标 `highlighted_candidate_index`，它对应的**表内绝对下标**就是 `page_size + highlighted_candidate_index`。光标必须设到这个绝对下标，才能正确高亮用户当前指向的那个真实候选。非最后一页没有前置占位，所以直接用页内下标即可。

**方向**：

[rime_engine.c:496-497](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L496-L497) —— `ibus_lookup_table_set_orientation(table, g_ibus_rime_settings.lookup_table_orientation)`。这个值来自 `ibus_rime.yaml` 的 `style/horizontal`：

[rime_settings.c:79-83](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L79-L83) —— `horizontal: true` → `IBUS_ORIENTATION_HORIZONTAL`（横排），`false` → `IBUS_ORIENTATION_VERTICAL`（竖排），未配置则保持默认 `IBUS_ORIENTATION_SYSTEM`（跟随系统）。

> 与 u3-l2 的呼应：同一个 `style/horizontal` 还在 `process_key_event` 里被用来同步 librime 的 `_horizontal` 选项（[rime_engine.c:533-538](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L533-L538)），目的是让方案切换器上下文里方向键的「上下/左右」语义与候选表方向一致（这正是 1.6.0/1.6.1 那次 arrow key orientation 修复的点）。也就是说，一个配置同时驱动了「librime 内部方向键语义」和「IBus 面板显示方向」两件事。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手在源码上标注三类 label 的优先级判断，并讲清「最后一页插入 `page_size` 个空候选再设 cursor_pos」的因果关系。

**操作步骤**：

1. **标注 label 优先级**：打开 [rime_engine.c:471-481](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L471-L481)，在三个分支各行起始处分别加注释：
   - 第 472-474 行 `if (i < context.menu.page_size && has_labels)` → `/* L1: 方案自定义标签 select_labels，仅前 page_size 项 */`
   - 第 475-477 行 `else if (i < num_select_keys)` → `/* L2: 选择键 select_keys 第 i 字符 */`
   - 第 478-480 行 `else` → `/* L3: 数字回退 (i+1)%10 */`

2. **手推最后一页的表格布局**：设 `page_size = 5`，当前是最后一页 `page_no = 2`，本页有 3 个真实候选，`highlighted_candidate_index = 1`（高亮第 2 个）。在纸上画出表内下标 → 内容的对照。

3. **验证 cursor_pos**：对照 [rime_engine.c:487-491](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L487-L491)，确认光标值。

**需要观察的现象（推理结果）**：

阶段 A 先插入 5 个空候选（下标 0..4），阶段 B 插入 3 个真实候选（下标 5,6,7），阶段 C 因 `has_page_down` 为假不插入。表格如下：

| 表内下标 | 内容 | 说明 |
| --- | --- | --- |
| 0..4 | （空） | 前置占位，制造「幽灵上一页」 |
| 5 | 真实候选 0 | |
| 6 | 真实候选 1 ← 高亮 | `highlighted_candidate_index = 1` |
| 7 | 真实候选 2 | |

`cursor_pos = page_size + highlighted_candidate_index = 5 + 1 = 6`，正好落在高亮项上。

**预期结果 / 你应能回答的问题**：

> 「为何最后一页要先插入 `page_size` 个空候选再设 cursor_pos？」

**答**：分两层。

- **为何插入空候选**：ibus-rime 每次只把当前页喂给 IBus，IBus 默认看不到「前面还有页」。在最后一页（且非首页）前置 `page_size` 个空候选，等于凭空造出一整页「幽灵上一页」，诱导 IBus 面板渲染「上一页」导航（对应注释 "show page up for last page"）。同理，非最后一页在尾部追加 1 个空候选以显示「下一页」。
- **为何 cursor_pos 要加 `page_size`**：前置占位把所有真实候选的整体下标平移了 `page_size`，高亮项从页内下标 `highlighted_candidate_index` 变成了表内绝对下标 `page_size + highlighted_candidate_index`。若不加这个偏移，光标会落在前面的空占位上，用户看到的高亮就错了。

> 关于「空占位如何具体触发 IBus 面板画出箭头」的精确机制依赖 IBus 面板内部实现，本环境无法运行 IBus 验证，记为「待本地验证」；但「占位导致下标平移、进而要求 cursor_pos 偏移」这一因果是代码本身确定的。

#### 4.4.5 小练习与答案

**练习 1**：`round` 在哪些页为真？为什么首尾页要关掉它？

**参考答案**：`round = !(is_last_page || page_no == 0)`，仅在「中间页」（非首页、非末页）为真。首尾页关闭 `round` 是因为这两页的翻页导航已经由「占位候选」负责表达（首页可能显示「下一页」、末页可能显示「上一页」），若再开启循环会让 IBus 面板的导航语义与占位策略打架。

**练习 2**：若把 [rime_engine.c:490](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L487-L491) 的 `page_size +` 去掉，在最后一页会出现什么现象？

**参考答案**：光标会指向 `highlighted_candidate_index` 这个下标，但该下标在前置占位区（0..page_size-1，都是空候选），所以高亮会落在某个空候选上，真实高亮项反而没有被标记，用户看到的高亮位置与实际不符。

**练习 3**：为什么非最后一页只在尾部追加 **1** 个空候选，而最后一页要在前面插入 **`page_size`** 个？

**参考答案**：尾部加 1 个空候选，只需让 IBus 面板察觉「候选数超过一页」即可触发「下一页」箭头，1 个就够。而最后一页要让光标定位正确、且面板认为当前页之前存在完整的一页，需要造出一整屏的「幽灵上一页」，所以是 `page_size` 个（这是 IBus 面板按 `page_size` 分页计算页码所要求的）。

---

### 4.5 鼠标点选与翻页回调

#### 4.5.1 概念说明

候选表不仅支持键盘选词，还支持两种交互：

- **鼠标点选** `candidate_clicked`：用户用鼠标点了一个候选，IBus 回调引擎，传入被点候选在**当前可见页内的下标** `index`。
- **翻页** `page_up` / `page_down`：用户翻了页（点面板箭头或按翻页键），IBus 回调引擎。

这两类回调的共同点是：**ibus-rime 不自己管理候选状态，而是把动作翻译成 librime 的按键事件，再走统一的 update 投影**。这正体现了「薄前端」——连鼠标点选都要绕回 librime。

#### 4.5.2 核心流程

鼠标点选：

```
candidate_clicked(index):
  if librime 支持 select_candidate:                       # 版本检查
      get_context → 若无候选则返回
      global_index = page_no * page_size + index          # 页内下标 → 全局下标
      rime_api->select_candidate(session, global_index)   # 让 librime 选中
      update()                                            # 投影新状态
```

翻页：

```
page_up:
  rime_api->process_key(session, IBUS_KEY_Page_Up, 0)     # 当成按键交给 librime
  update()

page_down:
  rime_api->process_key(session, IBUS_KEY_Page_Down, 0)
  update()
```

#### 4.5.3 源码精读

**鼠标点选**：

[rime_engine.c:577-596](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L577-L596) —— 三个要点：

- `RIME_API_AVAILABLE(rime_api, select_candidate)`：运行时检查当前 librime 版本是否提供 `select_candidate` 函数（老的 librime 可能没有），这是跨版本兼容的标准守卫。
- `page_no * page_size + index`：**下标换算是核心**。IBus 给的 `index` 是「当前可见页内的下标」，但 librime 的 `select_candidate` 要的是「跨所有页的全局下标」。所以乘上 `page_no * page_size` 把页内坐标还原成全局坐标。这与 4.4 节的 cursor_pos 偏移是「互逆」问题：一个是从全局/页内往表内绝对下标映射，一个是从页内往全局映射。
- 选中后照例调用 `ibus_rime_engine_update`，让 librime 的新状态（已上屏、新候选页等）投影到 UI。

> 小提示：这里取 `context` 只是为了拿 `page_no` 和 `page_size` 做换算，因此要 `get_context` / `free_context` 配对，不能泄漏。

**翻页**：

[rime_engine.c:598-610](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L598-L610) —— 翻页实现得非常简洁：直接把 `IBUS_KEY_Page_Up` / `IBUS_KEY_Page_Down` 当成普通按键交给 `rime_api->process_key`，modifers 传 0。librime 收到这两个键值后会自己翻候选页，然后 update 把新的一页投影出来。也就是说，ibus-rime 完全复用了 librime 的翻页逻辑，没有自己维护任何翻页状态。

> 这也解释了为什么 4.4 节里 IBus 面板的翻页箭头只是「触发回调」的视觉提示——真正的翻页计算在 librime 里，ibus-rime 每次只展示 librime 给出的当前页。

#### 4.5.4 代码实践

**实践目标**：跟踪一次鼠标点选的完整数据流，理解「页内下标 → 全局下标」的换算。

**操作步骤**：

1. 阅读 [rime_engine.c:583-594](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L583-L594)。
2. 假设 `page_size = 5`，当前 `page_no = 3`（第 4 页），用户用鼠标点了本页第 2 个候选（IBus 传入 `index = 1`）。
3. 手算传给 `select_candidate` 的全局下标。

**需要观察的现象（推理）**：`global_index = 3 * 5 + 1 = 16`，即 librime 会选中全局第 17 个候选。

**预期结果**：验证「鼠标点选必须把页内下标换算成全局下标」，否则在非首页点选会选错词。

> 若想本地验证：在 [rime_engine.c:592](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L590-L592) 的 `select_candidate` 调用前加 `g_message("click index=%u global=%u", index, context.menu.page_no * context.menu.page_size + index);`，运行后在翻到非首页时点击候选，查看日志。该运行结果「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `candidate_clicked` 不直接把 IBus 给的 `index` 传给 librime，而要先做 `page_no * page_size + index` 换算？

**参考答案**：IBus 的 `index` 是「当前可见页内的下标」（0..num_candidates-1），而 librime 的 `select_candidate` 期望「跨所有页的全局候选下标」。如果不换算，在 `page_no > 0` 的页上点击，会选到全局第 `index` 个候选（即第一页的某个词），完全选错。

**练习 2**：翻页回调为什么不用维护任何「当前页」状态？

**参考答案**：因为 ibus-rime 把 `Page_Up` / `Page_Down` 直接当成按键交给 librime 的 `process_key`，翻页的真正状态（当前第几页、每页几个）由 librime 在会话里维护。ibus-rime 每次按键后调 `update`，从 `get_context` 拿到 librime 算好的新当前页直接展示，自己不需要、也不应该保存翻页状态，否则会和 librime 的状态不一致（双写状态是薄前端的大忌）。

---

## 5. 综合实践

**任务**：在脑中（或纸上）完整复原一次「翻到最后一页并鼠标点选」的全过程，把本讲五个模块串起来。

情景设定：方案 `page_size = 5`，用户输入一串拼音，librime 给出多页候选。用户一路按下一页，翻到最后一页 `page_no = 2`（共 3 页），最后一页只有 3 个真实候选，`highlighted_candidate_index = 0`，所有候选都带注释，方案未提供 `select_labels` 但提供了 `select_keys = "12345"`。

请按顺序回答并对应到源码行号：

1. **方向**：若用户在 `ibus_rime.yaml` 设了 `style/horizontal: true`，候选表会怎么显示？对应 [rime_engine.c:496-497](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L496-L497) 与 [rime_settings.c:79-83](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L79-L83)。
2. **占位与 round**：本次 `has_page_up`、`has_page_down`、`round` 各是多少？会插入多少个空候选、插在哪里？对应 [rime_engine.c:439-451](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L439-L451)。
3. **候选着色**：每个真实候选的正文与注释如何拼接、哪段变灰？对应 [rime_engine.c:456-466](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L456-L466)。
4. **label**：3 个真实候选的标签分别是什么？（提示：`has_labels` 为假，走第 2 级。）对应 [rime_engine.c:471-481](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L471-L481)。
5. **cursor_pos**：光标值是多少？对应 [rime_engine.c:487-495](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L487-L495)。
6. **鼠标点选**：若用户点击本页第 3 个候选（IBus `index = 2`），传给 `select_candidate` 的全局下标是多少？对应 [rime_engine.c:583-594](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L583-L594)。

**参考答案**：

1. 横排（`IBUS_ORIENTATION_HORIZONTAL`）。
2. `has_page_up = (is_last_page && page_no>0) = 真`；`has_page_down = !is_last_page = 假`；`round = !(真 || 假) = 假`。会在真实候选**前**插入 `page_size = 5` 个空候选，尾部不插。
3. 每个候选显示为 `正文 注释`，其中「空格 + 注释」区间（`[text_len, end_index)`）着 `RIME_COLOR_DARK` 灰色。
4. `has_labels` 假 → 跳过第 1 级；`i=0,1,2 < num_select_keys=5` → 走第 2 级，标签为 `'1' '2' '3'`（`select_keys` 的前 3 个字符）。
5. `has_page_up` 为真，`cursor_pos = page_size + highlighted_candidate_index = 5 + 0 = 5`（落在第一个真实候选上）。
6. `global_index = page_no * page_size + index = 2*5 + 2 = 12`。

完成此实践后，你应该能把「配置 → 占位 → 着色 → 标签 → 光标 → 点选」整条链路在脑中跑通。

## 6. 本讲小结

- 候选表 `IBusLookupTable` 是引擎长期持有的对象（[init 创建](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L108-L109)），每次 update 先 `clear` 再重填，最后 `update_lookup_table` 提交。
- 候选正文与注释用 `g_strconcat` 拼成 `"text comment"`，注释段（含分隔空格）用 `RIME_COLOR_DARK` 着灰，区间按**字符**下标 `[text_len, end_index)` 计算。
- 候选序号 label 有三级优先级：方案自定义 `select_labels`（仅前 `page_size` 项）→ 选择键 `select_keys`（前 `num_select_keys` 项）→ 数字 `(i+1)%10`。
- 因每次只喂当前页给 IBus，ibus-rime 用**空占位候选**诱导面板画翻页箭头：末页前置 `page_size` 个、非末页尾部 1 个；`round` 仅中间页开启。
- 末页前置占位使真实候选下标整体平移 `page_size`，故 `cursor_pos = page_size + highlighted_candidate_index`；非末页则直接用页内下标。
- 鼠标点选 `candidate_clicked` 须把页内 `index` 换算成全局 `page_no * page_size + index` 再交给 librime；翻页则把 `Page_Up/Down` 当按键直接转发给 librime，自己不存翻页状态——全程「薄前端投影」。

## 7. 下一步学习建议

本讲讲完了 U4「前端 UI 渲染」三件事（状态同步、预编辑/辅助文本、候选表）中的最后一件。接下来：

- **进入 U5 配置系统**：本讲多次提到 `g_ibus_rime_settings.lookup_table_orientation`、`color_scheme` 等，下一讲 [u5-l1](u5-l1-yaml-config-loading.md) 会完整讲解 `ibus_rime.yaml` 是如何被 `rime_settings.c` 解析并填入这个全局结构的。建议接着读，把「配置 → 全局设置 → 渲染」的源头补齐。
- **回头验证**：若你已在本地跑起 ibus-rime，建议带着本讲的「修改参数并观察」实践，实际验证占位候选与 cursor_pos 的行为；这是理解 4.4 节最有效的方式。
- **延伸阅读**：本讲的占位/光标逻辑较为取巧，如果你想理解 IBus 面板**为什么**靠候选数与光标位置决定翻页箭头，可以阅读 IBus 源码中 `IBusLookupTable` 的 `page_up`/`page_down`/光标可见性判断部分（项目外依赖，不在本仓库内）。
- **U6 预告**：学完 U5 后，[u6-l2](u6-l2-architecture-and-extension.md) 会从架构层面回顾「薄前端 + RimeApi 稳定边界」，并给出新增 style 选项、状态栏按钮、按键处理的二次开发路径——本讲的候选表渲染正是其中一个可扩展点。
