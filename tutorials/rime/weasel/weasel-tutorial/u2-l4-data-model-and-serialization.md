# 数据模型与 boost 序列化

> 讲义 id：`u2-l4` ｜ 阶段：intermediate ｜ 依赖：`u2-l1`（IPC 接口与命令协议）

## 1. 本讲目标

本讲钻进 IPC 的「货物本身」——前后端之间到底在传什么。

读完后你应当能够：

1. 说出 `Context`、`Status`、`CandidateInfo`、`Text` 各自存放什么信息，以及它们之间的包含关系。
2. 看懂 `UIStyle` 里几十个字段分别控制候选窗口的哪一处外观，并能把它们按「字体 / 行为 / 图标 / 布局 / 配色」分类。
3. 解释 `boost::serialization::serialize` 模板是如何把一个 C++ 结构体变成可穿过命名管道的字节流的，并指出**哪些结构走 boost 序列化、哪些不走**。
4. 读懂服务端「写」与服务端「读」两侧的对应代码，能在「字段改动」时知道要同步修改哪几处。

---

## 2. 前置知识

### 2.1 序列化是什么、为什么需要它

进程之间不能用指针传对象——`WeaselTSF`（前端 DLL）和 `WeaselServer`（后台服务）是两个独立进程，对象在各自内存里地址不同。要把一个结构体（比如候选词列表）从一端送到另一端，必须先把它「拍扁」成一串字节，塞进管道；另一端读出来再「组装」回结构体。这个拍扁/组装的过程就是**序列化（serialization）**与**反序列化（deserialization）**。

Weasel 选择了 [boost::serialization](https://www.boost.org/doc/libs/release/libs/serialization/) 库来做这件事，原因有二：

- librime 本身依赖 boost，Weasel 也大量使用 boost（线程、管道缓冲流等），引入零成本。
- boost 序列化支持**递归组合**：只要给每个结构体写一个 `serialize` 模板，嵌套结构（如 `CandidateInfo` 里装着 `vector<Text>`，`Text` 里装着 `vector<TextAttribute>`）会被自动逐层处理。

### 2.2 boost 序列化的核心写法

boost 序列化的精髓是这样一个模板函数（签名固定）：

```cpp
template <typename Archive>
void serialize(Archive& ar, MyStruct& s, const unsigned int version) {
  ar & s.field_a;
  ar & s.field_b;
  // ...
}
```

这里的 `ar & s.field_a` 是一个**双向运算符**：

- 当 `Archive` 是「输出归档」（`text_woarchive`）时，`ar & x` 等价于 `ar << x`，把 `x` 写进字节流。
- 当 `Archive` 是「输入归档」（`text_wiarchive`）时，`ar & x` 等价于 `ar >> x`，从字节流读回 `x`。

所以**一个 `serialize` 模板同时承担序列化和反序列化两份职责**——这正是它能用一份代码在前后端复用的关键。`version` 参数用于版本演进，Weasel 里没有用到，固定忽略即可。

### 2.3 与 u2-l1 的衔接

u2-l1 讲了 IPC 的命令枚举、`PipeMessage` 三字段定长头、`EatLine` 回调。当时强调：定长头只够传「命令 + 两个整数」，**真正的业务载荷（候选词、写作串、样式）走的是变长正文**。本讲就回答：这个变长正文里装的是什么结构、怎么编码。

> ⚠️ 一个关键区分（本讲会反复强调）：`Context`、`Status`、`Config` **没有** `serialize` 模板，它们走「逐字段文本协议」（u2-l5 讲）；只有 `UIStyle`、`CandidateInfo` 以及 `CandidateInfo` 内部嵌套的 `Text` / `TextAttribute` / `TextRange` **有** `serialize` 模板，走 boost 序列化。

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，再辅以「写端」「读端」各一处对照。

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `include/WeaselIPCData.h` | **数据契约**（前后端共享的唯一头文件） | 全部结构体定义 + 全部 `serialize` 模板 |
| `RimeWithWeasel/RimeWithWeasel.cpp` | **写端**（服务端，把结构序列化进响应正文） | `text_woarchive` 两处调用 |
| `WeaselIPC/Styler.cpp` | **读端**（客户端，反序列化 `UIStyle`） | `text_wiarchive` 调用 |
| `WeaselIPC/ContextUpdater.cpp` | **读端**（客户端，反序列化 `CandidateInfo`） | `text_wiarchive` 调用 |
| `WeaselIPC/Deserializer.h` | 读端错误兜底 | `TryDeserialize` 模板 |
| `test/TestResponseParser/TestResponseParser.cpp` | 字段语义的参照用例 | 响应文本如何映射到结构体字段 |

`include/WeaselIPCData.h` 是前后端「共同的字典」——服务端的 `WeaselServer`（链接 `RimeWithWeasel` + `WeaselIPCServer`）和客户端的 `WeaselTSF`（链接 `WeaselIPC`）都包含它，因此双方对同一个结构体的内存布局理解一致。这也是 IPC 能跨进程工作的前提。

---

## 4. 核心概念与源码讲解

本讲分三个最小模块：

- **4.1** 核心数据结构：`Text` / `CandidateInfo` / `Context` / `Status`
- **4.2** `UIStyle` 样式字段全集
- **4.3** boost 序列化模板

---

### 4.1 核心数据结构：Text / CandidateInfo / Context / Status

#### 4.1.1 概念说明

打字时，输入法需要在前端和后端之间交换两类信息：

1. **「这次按键算出了什么」**——即写作串（preedit，比如拼音输入时的 `ni'hao`）、候选词列表（candies，比如「你好」「泥嚎」）、辅助提示（aux）。这些信息随每次按键变化，由 `Context` 承载。
2. **「输入法当前处于什么状态」**——即当前是中文还是英文（`ascii_mode`）、是否正在写作（`composing`）、当前方案名等。这些信息变化较慢，由 `Status` 承载。

为什么要把它们分开？因为 `Context` 几乎每次按键都变（要频繁刷新候选窗口），而 `Status` 只在切换方案/开关时变。分开存放让 UI 层可以判断「只更新候选」还是「连状态栏图标也要刷新」，避免无谓重绘。

`Context` 里嵌套的 `CandidateInfo` 是「一整页候选」的完整描述；每个候选项本身是一个 `Text`（因为候选项也带高亮属性）。`Text` 又由纯字符串 `str` 和一组 `TextAttribute`（高亮区间）组成。于是形成一条嵌套链。

#### 4.1.2 核心流程：包含关系树

可以用下面这棵树记住它们的嵌套关系：

```
Context
├── preedit : Text          # 写作串（光标处的预输入）
│   ├── str : wstring
│   └── attributes : vector<TextAttribute>
│       └── TextAttribute
│           ├── range : TextRange { start, end, cursor }
│           └── type  : TextAttributeType { NONE | HIGHLIGHTED }
├── aux : Text              # 辅助提示（同上结构）
└── cinfo : CandidateInfo   # 一整页候选
    ├── currentPage / totalPages / highlighted / is_last_page
    ├── candies  : vector<Text>    # 候选词正文
    ├── comments : vector<Text>    # 每个候选的注释（如拼音/词性）
    └── labels   : vector<Text>    # 每个候选的序号标签（1. 2. 3.）

Status                       # 与 Context 平级，由 ime 管理
├── schema_name / schema_id  # 当前输入方案
├── ascii_mode               # 中/英
├── composing                # 是否正在写作
├── disabled                 # 维护模式（暂停输入）
├── full_shape               # 全/半角
└── type : IconType { SCHEMA | FULL_SHAPE }

Config                       # 给前端的少量设置
└── inline_preedit : bool
```

记忆口诀：**「Context 描写一次按键的产出，Status 描写输入法的稳态，CandidateInfo 是 Context 里最复杂的子结构」**。

#### 4.1.3 源码精读

**最底层的砖块：`TextRange` 与 `TextAttribute`。** 一个「文本区间」用三个整数表示——起点 `start`、终点 `end`、光标位置 `cursor`：

[include/WeaselIPCData.h:12-25](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L12-L25) 定义了 `TextRange`，并给出了默认构造（`cursor(-1)` 表示无光标）和 `==`/`!=` 比较。它上面再包一层「属性类型」就是 `TextAttribute`：

[include/WeaselIPCData.h:27-39](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L27-L39) 把 `TextRange` 与一个 `TextAttributeType` 组合起来。类型枚举见 [include/WeaselIPCData.h:10](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L10)：目前只有 `NONE` 和 `HIGHLIGHTED`（高亮）两种，`LAST_TYPE` 是哨兵，留作后续扩展。

**带属性的字符串：`Text`。**

[include/WeaselIPCData.h:41-69](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L41-L69) 把一个 `std::wstring str` 和一个 `std::vector<TextAttribute> attributes` 放在一起。比如写作串 `ni'hao` 里，`n~i` 这段可能被标成高亮（表示当前正在匹配的部分），这就是靠往 `attributes` 里塞一个 `TextAttribute{0, 2, -1, HIGHLIGHTED}` 实现的。`operator==`/`operator!=` 逐项比较字符串和每个属性，用于 UI 层判断「内容没变就不重绘」。

**一整页候选：`CandidateInfo`。** 这是 `Context` 里信息量最大的子结构：

[include/WeaselIPCData.h:71-119](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L71-L119) 定义了它。数据成员集中在 [include/WeaselIPCData.h:112-118](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L112-L118)：

- `currentPage` / `totalPages` / `is_last_page`：分页状态。
- `highlighted`：当前高亮第几个候选（键盘选中的那个）。
- `candies`：候选词正文数组（注意这里是 `candies` 而非 `candidates`，全文一致，阅读源码时认这个拼写）。
- `comments`：每个候选的注释数组（拼音、词性等）。
- `labels`：每个候选的序号标签数组（`1.` `2.` `3.`）。

三个数组**长度一致、按下标对齐**——第 `i` 个候选的正文、注释、标签分别是 `candies[i]`、`comments[i]`、`labels[i]`。

**写作上下文：`Context`。**

[include/WeaselIPCData.h:121-146](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L121-L146) 把 `preedit`、`aux`、`cinfo` 三个字段组合起来（[include/WeaselIPCData.h:143-145](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L143-L145)）。注意它**没有** `serialize` 模板，因此 `Context` 整体不走 boost——它的 `preedit`/`aux` 走逐字段文本协议，而 `cinfo` 单独走 boost（见 4.3）。

**稳态：`Status`。**

[include/WeaselIPCData.h:150-186](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L150-L186) 定义了它，注释里写「由 ime 管理」。数据成员见 [include/WeaselIPCData.h:172-185](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L172-L185)：方案名、方案 id、四个 bool 开关、一个图标类型枚举 `IconType`（[include/WeaselIPCData.h:148](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L148)，`SCHEMA` 或 `FULL_SHAPE`，决定托盘/状态栏显示哪种图标）。`Status` 同样**没有** `serialize` 模板，走文本协议。

**还有一个小结构 `Config`**（[include/WeaselIPCData.h:189-193](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L189-L193)），只有一个 `inline_preedit` 布尔，告诉前端是否把写作串直接写到应用文档里（内联模式），它也走文本协议。

#### 4.1.4 代码实践：从测试用例反推字段语义

**实践目标**：用一个现成的测试用例，把「响应文本」与「结构体字段」对上号，巩固对 `Context`/`CandidateInfo` 字段含义的理解。

**操作步骤**：

1. 打开 [test/TestResponseParser/TestResponseParser.cpp:55-86](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestResponseParser/TestResponseParser.cpp#L55-L86) 的 `test_4()`。
2. 阅读它构造的响应字符串 `resp`，逐行写出该行最终会填到哪个结构体字段。

**需要观察的现象**：响应里每一行 `key=value` 都会被解析器拆成 `key` 路径与 `value`，例如：

- `ctx.preedit=候選乙=3.14` → `Context.preedit.str`
- `ctx.preedit.cursor=0,3` → 往 `Context.preedit.attributes` 塞一个 `HIGHLIGHTED` 属性，`range.start=0, range.end=3`
- `ctx.cand.0=候選甲` → `Context.cinfo.candies[0].str`
- `ctx.cand.cursor=1` → `Context.cinfo.highlighted=1`
- `ctx.cand.page=0/1` → `Context.cinfo.currentPage=0, totalPages=1`

**预期结果**：断言 `c.candies[0].str == L"候選甲"`、`c.highlighted == 1`、`c.currentPage == 0`、`c.totalPages == 1` 全部成立（见文件第 80–85 行）。

> ⚠️ 重要说明：该测试用例使用的是**较早的「逐字段文本协议」**写法（`ctx.cand.0=`、`ctx.cand.length=`、`ctx.cand.page=` 等多行）。**当前生产代码**已改为把整个 `CandidateInfo` 序列化成**一行** `ctx.cand=<boost 归档>`（见 4.3.3 的 [RimeWithWeasel.cpp:884-892](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L884-L892)）。因此这个测试更适合用来理解**字段语义**，并不直接对应现行协议格式；两者是否还能跑通「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CandidateInfo` 里要把候选正文、注释、标签拆成三个并列数组，而不是合并成一个「候选结构体数组」？

> **参考答案**：拆成三个数组可以让序列化更直接（boost 对 `vector<Text>` 原生支持），也方便 UI 层按需取用——例如 `comment_font_point==0` 时整列注释都不画（见 `RimeWithWeasel.cpp` 中 `comment_valid` 的判断）。同时三个数组共用同一套 `Text` 序列化模板，复用性更好。

**练习 2**：`TextRange` 的 `cursor` 默认值为什么是 `-1`？

> **参考答案**：`-1` 是哨兵值，表示「该区间没有光标」。`TextAttribute` 构造时把 `cursor` 传成 `-1`（[include/WeaselIPCData.h:29-30](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L29-L30)），因为普通高亮属性只关心 `[start, end)` 区间，不需要光标；只有写作串本身的光标位置才需要有效值。

---

### 4.2 UIStyle 样式字段全集

#### 4.2.1 概念说明

`UIStyle` 是 Weasel 里**最大的结构体**——一个结构体塞了约 80 个字段。它为什么这么大？因为小狼毫的候选窗口外观高度可定制：字体、字号、圆角、阴影、配色、布局、图标、行间距……几乎每一处像素都可以由用户通过 `weasel.custom.yaml` 调整。所有这些可调项都被收口进一个 `UIStyle`，由服务端（`RimeWithWeasel`，最清楚当前方案该用什么样式）读取配置、填好字段，再序列化发给前端（`WeaselUI` 负责按这些字段画窗口）。

换句话说，`UIStyle` 是「**服务端决策、前端执行**」的样式契约」：服务端决定「长什么样」，前端只管「照着画」。这样前端 DLL 就不必自己读 yaml、不必关心方案切换逻辑。

#### 4.2.2 核心流程：把 80 个字段分成六组

直接背 80 个字段没意义。按用途分组后就好记了：

| 组 | 代表字段 | 控制什么 |
| --- | --- | --- |
| **A. 字体** | `font_face`、`font_point`、`label_font_face`、`comment_font_point`、`candidate_abbreviate_length` | 正文/标签/注释各用什么字体、多大字号、候选词最长截断 |
| **B. 行为开关** | `inline_preedit`、`display_tray_icon`、`paging_on_scroll`、`enhanced_position`、`click_to_capture`、`hover_type` | 是否内联写作、是否显示托盘图标、是否滚轮翻页等布尔行为 |
| **C. 图标路径** | `current_zhung_icon`、`current_ascii_icon`、`current_half_icon`、`current_full_icon`、`label_text_format`、`mark_text` | 自定义状态图标文件、标签格式串（如 `%s.`）、高亮标记符 |
| **D. 布局** | `layout_type`、`align_type`、`min_width/max_width`、`margin_x/y`、`spacing`、`candidate_spacing`、`hilite_padding_x/y`、`round_corner`、`vertical_auto_reverse` | 横排/竖排/全屏、对齐、留白、间距、圆角 |
| **E. 配色** | `text_color`、`back_color`、`candidate_text_color`、`hilited_back_color`、`shadow_color`、`border_color` 等 ~20 个 | 文字/背景/候选/高亮/阴影/边框的 ARGB 颜色 |
| **F. per-client** | `client_caps`、`baseline`、`linespacing` | 客户端能力位、基线、行距 |

布局和配色还各有一套**枚举**约束取值，集中定义在结构体开头：

- `AntiAliasMode`（[include/WeaselIPCData.h:196-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L196-L202)）：抗锯齿模式，`DEFAULT`/`CLEARTYPE`/`GRAYSCALE`/`ALIASED`。
- `PreeditType`（[include/WeaselIPCData.h:204](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L204)）：写作串显示什么，`COMPOSITION`/`PREVIEW`/`PREVIEW_ALL`。
- `HoverType`（[include/WeaselIPCData.h:205](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L205)）：鼠标悬停行为，`NONE`/`SEMI_HILITE`/`HILITE`。
- `LayoutType`（[include/WeaselIPCData.h:206-213](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L206-L213)）：布局种类，`LAYOUT_VERTICAL`/`LAYOUT_HORIZONTAL`/`LAYOUT_VERTICAL_TEXT`/两种全屏，`LAYOUT_TYPE_LAST` 是哨兵。
- `LayoutAlignType`（[include/WeaselIPCData.h:215](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L215)）：竖排时的对齐，`ALIGN_BOTTOM`/`ALIGN_CENTER`/`ALIGN_TOP`。

`UIStyle` 的构造函数（[include/WeaselIPCData.h:295-364](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L295-L364)）给每个字段一个「安全默认值」（颜色默认 `0`、bool 默认 `false`、布局默认竖排 `LAYOUT_VERTICAL`、标签格式默认 `L"%s."`），保证没显式配置时也能画出基本可用的窗口。

#### 4.2.3 源码精读

结构体整体在 [include/WeaselIPCData.h:195-425](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L195-L425)，其中：

- **字体组**：[include/WeaselIPCData.h:217-224](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L217-L224)
- **布局参数组**：[include/WeaselIPCData.h:243-266](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L243-L266)
- **配色组**：[include/WeaselIPCData.h:267-289](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L267-L289)
- **per-client 组**：[include/WeaselIPCData.h:290-293](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L290-L293)

特别注意 [include/WeaselIPCData.h:365-424](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L365-L424) 的 `operator!=`：它把**所有字段**逐一比较，只要有一个不同就返回 `true`。前端用它判断「新样式和旧样式是否不同」，不同才触发重绘——这是避免每次按键都重建 DirectWrite 资源的关键优化。

> 观察细节：`operator!=` 是手写的巨长表达式，**字段顺序与 `serialize` 模板里的顺序并不完全一致**（例如 `hover_type`、`align_type` 在 `operator!=` 里靠前，在 `serialize` 里靠后）。这说明这三个地方（成员声明、构造、`operator!=`、`serialize`）必须**人工保持同步**——新增一个字段时，四处都要改，这是 `UIStyle` 维护时最容易踩的坑。

#### 4.2.4 代码实践：UIStyle 字段速查表（本讲指定实践任务）

**实践目标**：从「颜色」「布局」「字体」三类各挑 5 个字段，整理一张「字段名 → 含义 → 取值/枚举」速查表，作为日后定制样式的查阅手册。

**操作步骤**：

1. 打开 [include/WeaselIPCData.h:217-293](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L217-L293)。
2. 按下表填写，含义一栏结合字段名与 4.2.2 的分组理解推断。

**预期结果**（参考答案，可直接核对）：

| 类别 | 字段名 | 含义 | 取值 / 枚举 |
| --- | --- | --- | --- |
| 颜色 | `text_color` | 写作串文字颜色 | 32 位 ARGB 整数（如 `0xFFFFFFFF`） |
| 颜色 | `back_color` | 候选窗口背景色 | ARGB 整数 |
| 颜色 | `hilited_candidate_back_color` | 当前高亮候选的背景色 | ARGB 整数 |
| 颜色 | `shadow_color` | 窗口阴影颜色 | ARGB 整数 |
| 颜色 | `border_color` | 窗口边框颜色 | ARGB 整数 |
| 布局 | `layout_type` | 候选排布方向 | `LayoutType`：横/竖/竖排文字/全屏 |
| 布局 | `align_type` | 竖排时的对齐方式 | `LayoutAlignType`：底/中/顶 |
| 布局 | `margin_x` / `margin_y` | 窗口内容到边缘的留白 | 像素整数 |
| 布局 | `candidate_spacing` | 候选之间的间距 | 像素整数 |
| 布局 | `round_corner` | 候选高亮块的圆角半径 | 像素整数 |
| 字体 | `font_face` | 正文候选项字体名 | 字体名字符串（如 `Microsoft YaHei`） |
| 字体 | `font_point` | 正文字号 | 磅值整数 |
| 字体 | `label_font_face` | 序号标签字体名 | 字体名字符串 |
| 字体 | `comment_font_point` | 注释字号（0 表示不画注释） | 磅值整数 |
| 字体 | `candidate_abbreviate_length` | 候选词过长时截断长度 | 字符数整数 |

**需要观察的现象**：填完后你会发现「颜色」字段全是 `int`（存 ARGB）、「布局」字段是 `int` 像素值加几个枚举、「字体」字段是 `wstring` 字体名加 `int` 字号。这正是序列化时它们被当作简单标量逐个写入的原因。

#### 4.2.5 小练习与答案

**练习 1**：用户在 `weasel.custom.yaml` 里新增了一个颜色字段 `myspecial_color`，只在 Weasel 内部使用。需要改动 `WeaselIPCData.h` 里的哪几处？

> **参考答案**：至少四处——(1) 在配色组 [include/WeaselIPCData.h:267-289](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L267-L289) 加成员声明；(2) 在构造函数 [include/WeaselIPCData.h:295-364](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L295-L364) 加默认值；(3) 在 `operator!=` [include/WeaselIPCData.h:365-424](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L365-L424) 加比较项；(4) 在 `serialize` 模板（[include/WeaselIPCData.h:429-503](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L429-L503)，4.3 详述）加 `ar & s.myspecial_color;`。漏掉 `serialize` 会导致字段无法跨进程传输；漏掉 `operator!=` 会导致样式变化检测失灵。

**练习 2**：`comment_font_point` 设为 `0` 代表什么？为什么需要这个约定？

> **参考答案**：代表「不显示注释」。因为 `CandidateInfo.comments` 数组里仍可能有内容，前端需要一个开关决定「画还是不画」。用字号 `0` 当哨兵，省得再加一个独立布尔字段，这是 `UIStyle` 里常见的「用数值兼当开关」的紧凑写法。

---

### 4.3 boost 序列化模板

#### 4.3.1 概念说明

有了前面的结构体，还差「怎么把它们变成字节」。这就是 `WeaselIPCData.h` 末尾、位于 `namespace boost::serialization` 下的那一组 `serialize` 模板（[include/WeaselIPCData.h:427-536](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L427-L536)）。

boost 序列化的规则是「侵入式 `serialize` 函数要放在 `namespace boost::serialization` 里，对目标类型重载」。Weasel 选择把 `serialize` 写在 `WeaselIPCData.h` 末尾、显式 `namespace boost::serialization { ... }` 中，这样：

- 它们与结构体定义在同一文件，改字段时顺手就能改序列化。
- 对 `weasel::UIStyle`、`weasel::CandidateInfo` 等类型形成重载，boost 归档对象遇到这些类型时自动调用对应模板。

Weasel 选的是 `text_woarchive` / `text_wiarchive`（宽字符**文本**归档），而不是二进制归档。原因有二：

1. **可读性**：文本归档输出的是空格分隔的可读 token，出问题时可以 `OutputDebugString` 直接打印响应正文排查，二进制归档则是一堆不可读字节。
2. **与管道缓冲流兼容**：宽字符文本归档能直接落到 u2-l2 讲过的 `boost::wbufferstream` 上，再写入命名管道。

#### 4.3.2 核心流程：一次序列化的完整往返

以「服务端把一页候选发给前端」为例，完整链路如下：

```
[服务端 RimeWithWeasel.cpp]
  1. 填好 CandidateInfo cinfo（candies/comments/labels/highlighted...）
  2. std::wstringstream ss;
     boost::archive::text_woarchive oa(ss);   // 建输出归档
     oa << cinfo;                              // 触发 serialize(cinfo) 模板
        └─ 内部递归：vector<Text> → Text → vector<TextAttribute> → ...
  3. body += L"ctx.cand=" + ss.str() + L"\n";  // 把整段归档文本作为「一行」嵌入响应

[管道传输 u2-l2]
  4. 整段响应正文（多行文本）经 PipeChannel 写入命名管道

[客户端 WeaselIPC]
  5. ResponseParser 按行切分，遇到 "ctx.cand=..." 这一行
  6. ContextUpdater::_StoreCand 取出等号后的 value（即归档文本）
     std::wstringstream ss(value);
     boost::archive::text_wiarchive ia(ss);   // 建输入归档
     ia >> cinfo;                              // 触发同一个 serialize 模板，反向读回
  7. cinfo 还原完成，交给 UI 层绘制
```

这条链路里有两个要点：

- **同一个 `serialize` 模板被调用两次**——写时 `ar & x` 走 `<<`，读时 `ar & x` 走 `>>`，靠 `Archive` 类型区分方向。这就是 boost 序列化「一份代码、双向使用」的原理。
- **递归组合**：`CandidateInfo` 的 `serialize`（[include/WeaselIPCData.h:505-516](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L505-L516)）里写了 `ar & s.candies;`，而 `candies` 是 `vector<Text>`。boost 对 `vector` 有内置支持（靠文件顶部 `#include <boost/serialization/vector.hpp>`，[include/WeaselIPCData.h:5](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L5)），它会逐个元素调用 `Text` 的 `serialize`（[include/WeaselIPCData.h:517-521](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L517-L521)），`Text` 又调用 `TextAttribute`（[include/WeaselIPCData.h:522-528](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L522-L528)），层层下探到 `TextRange`（[include/WeaselIPCData.h:529-534](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L529-L534)）。你只需为每种类型写一个模板，嵌套关系由库自动展开。

**哪些结构有序列化模板、哪些没有**——这是一道分界线，务必记牢：

| 结构 | 有 `serialize` 模板？ | 传输方式 |
| --- | --- | --- |
| `UIStyle` | ✅ | 整体序列化为一行 `style=<归档>` |
| `CandidateInfo` | ✅ | 整体序列化为一行 `ctx.cand=<归档>` |
| `Text` | ✅（被上面两者递归调用） | 不单独传输 |
| `TextAttribute` | ✅（同上） | 不单独传输 |
| `TextRange` | ✅（同上） | 不单独传输 |
| `Context` | ❌ | 逐字段文本协议（`ctx.preedit=`、`ctx.aux=`） |
| `Status` | ❌ | 逐字段文本协议（`status.schema_id=` 等） |
| `Config` | ❌ | 逐字段文本协议（`config.inline_preedit=`） |

为什么 `Context`/`Status` 不走 boost？因为它们的字段大多是「一行就能写完的标量」（一个字符串、一个布尔），用文本协议逐行写更直观、也方便增量更新（这次只更新 `ascii_mode` 就只发那一行，不必把整个 `Status` 重发）。而 `CandidateInfo` 含变长数组、嵌套结构，用文本协议要拆成很多行且容易出错，所以整体序列化更划算。`UIStyle` 同理——80 个字段一次性序列化比写 80 行文本紧凑。

#### 4.3.3 源码精读：写端与读端

**写端①：序列化 `CandidateInfo`**（服务端 `_Respond` 内）。

[RimeWithWeasel/RimeWithWeasel.cpp:884-892](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L884-L892)：当本页有候选时，建一个 `text_woarchive`，`oa << cinfo` 把整个 `CandidateInfo` 序列化进 `wstringstream`，再以 `ctx.cand=<内容>` 的形式追加到响应正文。注意这里只写「key」`ctx.cand`，不带下标——整页候选就是一行。

**写端②：序列化 `UIStyle`**（服务端 `_Respond` 内）。

[RimeWithWeasel/RimeWithWeasel.cpp:902-911](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L902-L911)：仅当本会话样式尚未同步过（`!session_status.__synced`）时，才把 `session_status.style` 序列化成 `style=<内容>` 一行发出，发出后立刻置 `__synced=true`。这是一个**「样式只发一次」的优化**——样式不随每次按键变化，没必要每帧重发几十个字段。

**读端①：反序列化 `UIStyle`**（客户端 `Styler`）。

[WeaselIPC/Styler.cpp:11-21](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Styler.cpp#L11-L21)：收到 `style=` 这一行后，把等号后的 `value` 倒进 `wstringstream`，建 `text_wiarchive`，调用 `TryDeserialize(ia, sty)` 还原出 `UIStyle`。

**读端②：反序列化 `CandidateInfo`**（客户端 `ContextUpdater::_StoreCand`）。

[WeaselIPC/ContextUpdater.cpp:70-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ContextUpdater.cpp#L70-L84)：同样的套路还原 `CandidateInfo`，还原完再用 `unescape_string` 把候选词里的转义字符（换行、`=` 等）还原——因为整段归档是作为「一行」传输的，候选词里原本的换行必须先转义（见服务端 `escape_string` 调用），到了客户端再解回来。

**错误兜底：`TryDeserialize`**。

[WeaselIPC/Deserializer.h:7-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.h#L7-L16) 用 `try/catch` 包住 `ia >> t`，捕获 `boost::archive::archive_exception` 并弹框提示。这是因为前后端 `serialize` 模板**版本不一致**（比如服务端加了字段、客户端没更新）时，反序列化会抛异常；兜底成弹框而不是让进程崩溃，保证「服务端升级、客户端还是旧 DLL」时也能给出诊断信息而不是闪退。

**五个 `serialize` 模板本身**集中在 [include/WeaselIPCData.h:429-534](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L429-L534)。其中 `UIStyle` 的最长（[include/WeaselIPCData.h:429-503](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L429-L503)），把约 70 个字段依次 `ar &` 一遍；`CandidateInfo`（[include/WeaselIPCData.h:505-516](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L505-L516)）则把分页状态与三个 `vector<Text>` 依次写出。

> ⚠️ **字段顺序即协议**：`serialize` 模板里 `ar &` 的顺序，就是字节流里字段的排列顺序。**前后端必须完全一致**，否则读端会把 A 字段的值塞进 B 字段。所以新增字段时，最安全的做法是**追加在末尾**，不要插在中间——末尾追加能让旧客户端读到旧前缀、忽略新尾部，兼容性更好。

#### 4.3.4 代码实践：跟踪一次 CandidateInfo 的序列化往返

**实践目标**：把 4.3.2 讲的链路在源码里走一遍，亲眼看到「同一个 `serialize` 模板如何被写端和读端各调用一次」。

**操作步骤**：

1. 从写端开始：读 [RimeWithWeasel/RimeWithWeasel.cpp:884-892](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L884-L892)，确认 `oa << cinfo` 触发的是 [include/WeaselIPCData.h:505-516](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L505-L516) 的模板，并顺着 `ar & s.candies` 进入 `vector<Text>` → `Text`（[include/WeaselIPCData.h:517-521](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L517-L521)）→ `TextAttribute`（[include/WeaselIPCData.h:522-528](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L522-L528)）→ `TextRange`（[include/WeaselIPCData.h:529-534](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L529-L534)）的递归。
2. 跳到读端：读 [WeaselIPC/ContextUpdater.cpp:70-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ContextUpdater.cpp#L70-L84)，确认 `ia >> cinfo`（经 `TryDeserialize`）调用的是**同一个** [include/WeaselIPCData.h:505-516](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L505-L516) 模板，只是这次 `Archive` 是输入归档。
3. 数一数：一次 `oa << cinfo` 会按顺序写出多少个标量？

**需要观察的现象**：写端和读端**没有任何字段名**出现在字节流里——`text_woarchive` 输出的是纯粹的「值序列」。双方之所以能对上号，完全靠 `serialize` 模板里 `ar &` 的**顺序一致**。

**预期结果**：第 3 步的答案是——`CandidateInfo` 先写 4 个标量（`currentPage`、`totalPages`、`highlighted`、`is_last_page`），再写 3 个 `vector`（`candies`、`comments`、`labels`）；每个 `vector` 先写长度再逐个元素；每个 `Text` 元素先写 `str`（含长度前缀）再写 `attributes` 数组……（精确的字节数「待本地验证」，可自行写一个小程序 `text_woarchive` 一个 `CandidateInfo` 后打印 `ss.str()` 观察）。这一步若能本地跑通，对 boost 文本归档格式会有最直观的认识。

> 若无法本地编译，可改为「源码阅读型实践」：在 [include/WeaselIPCData.h:505-534](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L505-L534) 上按 `ar &` 出现顺序，手写出 `CandidateInfo` 序列化时的字段先后清单。

#### 4.3.5 小练习与答案

**练习 1**：假设服务端在 `UIStyle::serialize` 末尾新增了 `ar & s.new_flag;`，但客户端 DLL 还是旧的（没有这一行）。反序列化时会怎样？

> **参考答案**：客户端读时会少读一个字段，但因为新字段加在**末尾**，旧客户端读完它认识的字段就停止了，多出来的尾部被忽略——`UIStyle` 大部分字段仍正确，`new_flag` 在客户端保持默认值。如果新字段是**插在中间**，则从该字段起后面所有字段都会错位，可能触发 `archive_exception`，被 [WeaselIPC/Deserializer.h:7-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Deserializer.h#L7-L16) 的 `TryDeserialize` 兜底成弹框。这正是「新增字段应追加在末尾」的原因。

**练习 2**：为什么 `Context` 和 `Status` 不写 `serialize` 模板、改走文本协议？

> **参考答案**：两者字段多为单行标量，文本协议可逐字段增量更新（只发变化的部分），且人类可读便于调试；而整体 boost 序列化每次都要全量发送。`CandidateInfo`/`UIStyle` 因含变长数组/字段众多，全量序列化反而更紧凑、更不易错。这是按「结构复杂度」选择的混合策略。

**练习 3**：`text_woarchive` 与 `binary_oarchive` 相比，Weasel 选前者付出了什么代价、换来了什么？

> **参考答案**：代价是体积更大（文本 token 比二进制占空间，例如整数 `123456` 要 6 字节而非 4 字节）、解析稍慢。换来的是**可读性**（出 bug 能直接打印响应正文排查）和**与宽字符缓冲流的良好兼容**。对于输入法这种「单次载荷不大（受 u2-l1 的 4 KiB 缓冲框定）、调试成本高于性能成本」的场景，这个取舍是合理的。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**从结构体到字节流再回到结构体**」的完整盘点。

**任务**：以「用户按键后服务端返回一页候选」为场景，写一份「**数据生命周期表**」，要求每一行包含：① 涉及的结构体/字段；② 它在这一刻位于哪段代码；③ 用什么方式编码进响应正文。具体至少覆盖以下 6 个节点：

1. 服务端把候选词填进 `CandidateInfo.candies`（结构体层）。
2. 服务端 `oa << cinfo` 触发 [include/WeaselIPCData.h:505-516](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L505-L516)（序列化层）。
3. 序列化结果作为 `ctx.cand=` 一行嵌入响应（[RimeWithWeasel/RimeWithWeasel.cpp:884-892](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L884-L892)，协议层）。
4. 经命名管道传输（参考 u2-l2 的 `PipeChannel`）。
5. 客户端 `ContextUpdater::_StoreCand` 取出该行（[WeaselIPC/ContextUpdater.cpp:70-84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/ContextUpdater.cpp#L70-L84)，反序列化层）。
6. `unescape_string` 还原候选词，`cinfo` 交给 UI 层。

**预期产出**：一张六行表格，能清楚说明「同一个 `CandidateInfo` 在哪一刻是 C++ 对象、哪一刻是字节流、边界在哪两行代码」。做完后，再对照 4.2.4 的 `UIStyle` 速查表，思考：如果要把 `style=` 那一行也加进这张表，第 2、5 步对应的行号分别是什么？（答案：第 2 步是 [RimeWithWeasel/RimeWithWeasel.cpp:902-911](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L902-L911)，第 5 步是 [WeaselIPC/Styler.cpp:11-21](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/Styler.cpp#L11-L21)。）

---

## 6. 本讲小结

- `Context`（写作串 + 候选）与 `Status`（中/英、方案、开关）是前后端交换的两类核心信息；`CandidateInfo` 是 `Context` 里最复杂的子结构，`Text`/`TextAttribute`/`TextRange` 是更底层的「带属性字符串」砖块。
- `UIStyle` 汇集了约 80 个外观字段，按「字体 / 行为 / 图标 / 布局 / 配色 / per-client」六组记忆；它有一套嵌套枚举（布局、抗锯齿、对齐等）约束取值。
- boost 序列化靠一个 `serialize(Archive&, T&, version)` 模板同时承担「读」和「写」，`ar & x` 的方向由 `Archive` 类型决定；嵌套结构靠模板递归自动展开。
- **只有 `UIStyle`、`CandidateInfo`、`Text`、`TextAttribute`、`TextRange` 走 boost 序列化**，整体编码为一行（`style=`、`ctx.cand=`）；`Context`、`Status`、`Config` 走逐字段文本协议。
- 服务端用 `text_woarchive` 写（`RimeWithWeasel.cpp`），客户端用 `text_wiarchive` 读（`Styler.cpp` / `ContextUpdater.cpp`），`TryDeserialize` 兜底版本不一致异常。
- 维护要点：`UIStyle` 新增字段必须同步四处（声明、构造、`operator!=`、`serialize`），且 `serialize` 里的字段应**追加在末尾**以保证前后端兼容。

---

## 7. 下一步学习建议

本讲只讲了「数据结构长什么样、怎么序列化」，但刻意没讲「响应正文的多行协议本身是怎么解析的」——也就是 `action=ctx,style\n` 这种行格式如何被拆分、`ctx.preedit=` 如何路由到 `ContextUpdater`。这正是下一讲 **u2-l5《响应解析、反序列化与动作分发》** 的主题，它讲清 `ResponseParser` → `ActionLoader` → `Deserializer` 的分发机制，补上本讲省略的「文本协议」那一半。

进一步建议：

- 想看 boost 文本归档的真实输出长什么样，可写一个最小 Win32 控制台程序，`text_woarchive` 一个 `CandidateInfo` 后打印 `ss.str()`（注意链接 boost，配置参考 u1-l3）。
- 想理解 `UIStyle` 这些字段最终如何被画成像素，跳到 **u5《候选窗口 UI 渲染》**，特别是 u5-l2（布局系统）与 u5-l3（DirectWrite 绘制）。
- 想了解用户如何通过 yaml 改这些字段、改完如何流入 `UIStyle`，参考 **u7-l3《配色方案与样式定制实战》** 与 **u6-l1《WeaselDeployer 配置器》**。
