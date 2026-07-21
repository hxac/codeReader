# 配色方案与样式定制实战

## 1. 本讲目标

本讲是「二次开发」单元里最贴近用户可见效果的一篇。读完后你应当能够：

- 说清 `UIStyle` 里那二十多个颜色字段、圆角、阴影、字体字段分别画在候选窗口的哪个位置。
- 解释为什么 Weasel 要在 `RimeWithWeasel.cpp` 里做 `ARGB2ABGR` / `RGBA2ABGR` 转换，并能手算一个颜色值经过转换后的结果。
- 写出一份 `weasel.custom.yaml` 自定义配色，并说清它从「Deployer 写文件」→「Server 加载」→「boost 序列化进管道」→「前端 DirectWriteResources 画到屏幕」的完整链路。

本讲承接 u4-l3（配置三层来源）与 u5-l2（布局系统），把「配置」与「绘制」两端用「颜色」这一具体例子串起来。

## 2. 前置知识

阅读本讲前，最好已经了解以下概念（不熟悉也无妨，下面会顺带复习）：

- **COLORREF**：Windows 表示颜色的 32 位整数，字节序为 `0x00BBGGRR`，即最低字节是红（R）、中间是绿（G）、高位是蓝（B）。配套宏 `GetRValue(c)`、`GetGValue(c)`、`GetBValue(c)` 分别取出 R、G、B。
- **Alpha 通道**：表示透明度，0 为完全透明、255 为完全不透明。Weasel 把 alpha 放在 32 位整数的最高字节，于是颜色值形如 `0xAABBGGRR`。
- **GDI+ 与 DirectWrite**：WeaselUI 用两套绘图 API。GDI+（`Gdiplus::Color`）画背景圆角、阴影、边框；DirectWrite 画文字。两套 API 共用同一块内存 DC（见 u5-l3）。
- **librime 的 levers 覆盖层**：用户个性化以 `__patch` 写进 `*.custom.yaml`，叠加在发行版默认 `*.yaml` 之上（见 u6-l1）。`weasel.custom.yaml` 就是覆盖 `weasel.yaml` 的用户层。
- **`__synced` 惰性同步**：服务端 `SessionStatus` 里的 `__synced` 标志，用来决定本次按键响应是否要把整份 `UIStyle` 重新序列化发给前端（见 u4-l2、u4-l4）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/WeaselIPCData.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h) | 定义 `UIStyle` 结构体：颜色、圆角、阴影、字体、布局等全部外观字段，以及把它们序列化进管道的 `boost::serialization` 模板。 |
| [RimeWithWeasel/RimeWithWeasel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp) | 颜色格式转换宏、`_RimeGetColor` 解析器、`_UpdateUIStyleColor` 配色表、`blend_colors` alpha 混合；以及 `UpdateColorTheme` / `_LoadSchemaSpecificSettings` 等加载入口。 |
| [WeaselUI/WeaselPanel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp) | 把 `UIStyle` 里的颜色字段真正画上屏幕：`_TextOut` 把 COLORREF 转 DirectWrite 画刷，`_HighlightText` 画背景/阴影/边框。 |
| [WeaselDeployer/UIStyleSettings.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp) | Deployer 里读写 `style/color_scheme`、枚举 `preset_color_schemes`、把用户选择写进 `weasel.custom.yaml`。 |
| [output/data/weasel.yaml](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/data/weasel.yaml) | 发行版自带的样式默认值与若干内置配色（`aqua`、`psionics` 等），是本讲的真实示例来源。 |

---

## 4. 核心概念与源码讲解

### 4.1 UIStyle 颜色与外观字段语义

#### 4.1.1 概念说明

候选窗口长得什么样，几乎完全由一个叫 `UIStyle` 的结构体决定。它是一份「服务端决策、前端执行」的样式契约（见 u2-l4）：librime 所在的 `WeaselServer` 进程把它算好，经命名管道 `boost` 序列化整块发给前端的 `weasel.dll`，前端只负责照着画。

`UIStyle` 里的字段可以分成两大类：

- **颜色字段**：23 个 `int`，每一个对应一种「像素对象」的颜色，比如普通候选文字、高亮候选背景、编码区背景、阴影、边框等。
- **外观（几何）字段**：圆角半径、阴影半径与偏移、边框宽度、内边距、字号、字体名等。它们决定颜色「画成什么形状、多大、偏到哪里」。

理解这些字段的关键，是建立「字段名 → 候选窗口里的视觉对象」的映射。

#### 4.1.2 核心流程

一个候选窗口从上到下大致可以分成几层视觉对象：

```text
┌─────────────────────────────── 候选窗口（back_color 背景） ─┐
│  border_color 边框                                            │
│   ┌─ 编码区 / preedit（hilited_text_color 文字 / hilited_back_color 背景） ┐
│   │  ni'hao                                                   │
│   └──────────────────────────────────────────────────────────┘
│   ┌─ 高亮候选（hilited_candidate_* 颜色 + hilited_candidate_shadow_color 阴影）┐
│   │  1 你好                                                    │
│   └──────────────────────────────────────────────────────────┘
│   2 你嚎   candidate_text_color 文字 / candidate_back_color 背景 / candidate_shadow_color 阴影
│   3 尼号   label_text_color 序号 / comment_text_color 注释
│        shadow_color 整体外阴影
└──────────────────────────────────────────────────────────────┘
```

字段命名遵循一个很规律的「对象 + 状态」模式：

- 不带 `hilited_` 前缀 → 默认/普通状态（如 `candidate_text_color` 普通候选文字）。
- 带 `hilited_` 前缀 → 高亮（选中）状态（如 `hilited_candidate_text_color` 高亮候选文字）。
- `text_color` / `back_color` 指编码行（preedit）本身的文字与背景；`candidate_*` 指候选词条。

#### 4.1.3 源码精读

颜色字段全部集中在 `UIStyle` 的 `// color scheme` 段，类型统一是 `int`：

[include/WeaselIPCData.h:L267-L291](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L267-L291) — 这段声明了 23 个颜色字段，`text_color` 起到 `nextpage_color` 止。注意它们都是裸 `int`，语义上承载 `0xAABBGGRR` 布局（alpha 在最高字节）。

外观（几何）字段紧挨在前面，本讲重点用到圆角与阴影：

[include/WeaselIPCData.h:L261-L265](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L261-L265) — `round_corner`（高亮候选圆角）、`round_corner_ex`（普通候选/背景圆角）、`shadow_radius`（阴影模糊半径）、`shadow_offset_x/y`（阴影偏移）。

字体三件套与字号，决定文字用什么字形画多大：

[include/WeaselIPCData.h:L218-L223](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L218-L223) — `font_face` / `label_font_face` / `comment_font_face` 三套字体（正文/序号/注释），各自一个 `*_font_point` 字号。

这些字段如何跨进程传输？答案是整块 `boost` 序列化，颜色部分对应 `serialize` 模板里的 `// color scheme` 段：

[include/WeaselIPCData.h:L476-L498](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPCData.h#L476-L498) — `ar & s.text_color;` 这样逐字段 `&` 进宽字符文本归档。字段顺序就是协议顺序，新增颜色字段必须追加在末尾，否则破坏前后端兼容（见 u2-l4）。

> 字段名「`round_corner` 是高亮候选的圆角、`round_corner_ex` 才是普通候选/窗口背景的圆角」这一点容易记反。下面的实战会帮你用一份 yaml 验证它。

#### 4.1.4 代码实践

**实践目标**：用 `output/data/weasel.yaml` 里真实存在的 `aqua` 配色，建立「字段名 → 视觉对象」的确认表。

**操作步骤**：

1. 打开 [output/data/weasel.yaml:L66-L82](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/data/weasel.yaml#L66-L82)，这是 `aqua` 配色定义。
2. 对照上面的候选窗口示意图，把每个字段填进下表。

| yaml 字段 | 值 | 视觉对象 |
|-----------|-----|---------|
| `text_color` | `0x000000` | 编码行默认文字（黑） |
| `back_color` | `0xeceeee` | 候选窗整体背景（浅灰） |
| `hilited_candidate_back_color` | `0xfa3a0a` | 高亮候选背景（橙红） |
| `hilited_candidate_text_color` | `0xffffff` | 高亮候选文字（白） |
| `candidate_shadow_color` | `0x00000000` | 普通候选阴影（全透明＝无阴影） |
| `border_color` | `0xe0e0e0` | 候选窗边框 |

**需要观察的现象 / 预期结果**：注意 `aqua` 里所有阴影色都是 `0x00000000`（8 位写法，alpha=0），表示「完全透明、不画阴影」；而 `text_color: 0x000000` 是 6 位写法，会被代码补成不透明。这个「6 位补全 alpha、8 位原样保留」的差异，正是下一节颜色解析的核心。

> 本环境为 Linux，无法实际启动 Weasel 观察像素。上表对照是基于源码逻辑的预测，**实际像素效果待本地（Windows）验证**。

#### 4.1.5 小练习与答案

**练习 1**：`hilited_text_color` 和 `hilited_candidate_text_color` 有什么区别？

> **答案**：前者是「编码行（preedit）在高亮状态的文字颜色」，后者是「高亮候选词条的文字颜色」。编码行是用户正在敲的拼音串，候选词条是引擎给出的候选字；它们是窗口里两个不同区域。

**练习 2**：`label_text_color` 和 `comment_text_color` 分别画在哪？

> **答案**：`label_text_color` 是候选序号（1、2、3…）的颜色；`comment_text_color` 是候选项注释（如双拼方案里的拼音提示）的颜色。若 yaml 里没写，代码会用 `blend_colors` 把候选文字色与背景色按 alpha 混合出一个默认值（见 4.2.3）。

---

### 4.2 颜色格式转换：ARGB/RGBA → ABGR

#### 4.2.1 概念说明

这是本讲最容易踩坑、也最能体现「跨库协作」的一节。

问题来自一个现实冲突：

- **Windows 的 `COLORREF`** 字节序是 `0x00BBGGRR`，蓝在高位、红在低位。配套宏 `GetRValue(c)` 取的是最低字节（红）。
- **Web/CSS 习惯** 写成 `#RRGGBB`（ARGB 加 alpha 即 `0xAARRGGBB`），红在高位、蓝在低位。
- **Rime 的历史习惯** 沿用 `COLORREF` 风格，把 alpha 塞进最高字节，得到 `0xAABBGGRR`。

Weasel 的绘制代码（`GetRValue` / `GetBValue`）是按 Windows `COLORREF` 写的，所以内部统一采用 `0xAABBGGRR` 这一套，代码里命名为 `COLOR_ABGR`（最高字节到最低字节依次读作 A-B-G-R）。但如果用户在 yaml 里更习惯写 ARGB（`#RRGGBB` / `0xAARRGGBB`）或 RGBA，就必须做一次字节重排，把用户写的格式翻成内部 ABGR。这就是 `ARGB2ABGR` / `RGBA2ABGR` 两个宏存在的全部理由。

#### 4.2.2 核心流程

转换的数学本质是字节位置的重排。把一个 32 位颜色值按「最高字节→最低字节」记作 4 个字节 \(B_0B_1B_2B_3\)：

- **ARGB** = \(A\,R\,G\,B\)（\(B_0{=}A, B_1{=}R, B_2{=}G, B_3{=}B\)）
- **RGBA** = \(R\,G\,B\,A\)（\(B_0{=}R, B_1{=}G, B_2{=}B, B_3{=}A\)）
- **ABGR**（目标）= \(A\,B\,G\,R\)（\(B_0{=}A, B_1{=}B, B_2{=}G, B_3{=}R\)）

ARGB → ABGR 只需交换 R 与 B 两个字节（A、G 原地不动）：

\[
\mathrm{ARGB2ABGR}(v) = (v \,\&\, 0xFF000000) \;|\; ((v \,\&\, 0x000000FF) \ll 16) \;|\; (v \,\&\, 0x0000FF00) \;|\; ((v \,\&\, 0x00FF0000) \gg 16)
\]

RGBA → ABGR 是一次完整轮转：

\[
\mathrm{RGBA2ABGR}(v) = ((v \,\&\, 0xFF) \ll 24) \;|\; ((v \,\&\, 0xFF000000) \gg 24) \;|\; ((v \,\&\, 0x00FF0000) \gg 8) \;|\; ((v \,\&\, 0x0000FF00) \ll 8)
\]

解析与转换的整体流程是：

```text
yaml 字符串(如 "0xfa3a0a" / "#fff" / "argb:..." )
   │
   ▼
_RimeGetColor: 去掉 0x/# 前缀 → 展开 3/4 位简写 → stoul 转 int
   │   (6 位 → 补 0xff000000 全不透明; 8 位 → 原样)
   ▼
按 color_format(argb/rgba/abgr) 选择 ARGB2ABGR / RGBA2ABGR / 不变
   │
   ▼
得到内部 ABGR(0xAABBGGRR)，存进 UIStyle.*_color
```

#### 4.2.3 源码精读

三个宏与颜色格式枚举定义在文件顶部：

[RimeWithWeasel/RimeWithWeasel.cpp:L15-L22](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L15-L22) — `TRANSPARENT_COLOR`、`ARGB2ABGR`、`RGBA2ABGR` 三个位运算宏，以及 `ColorFormat { COLOR_ABGR, COLOR_ARGB, COLOR_RGBA }` 枚举。`COLOR_ABGR = 0` 是默认值，呼应 Rime 的历史习惯。

颜色解析器 `_RimeGetColor` 负责把字符串变成整数，关键在三段逻辑：

[RimeWithWeasel/RimeWithWeasel.cpp:L1017-L1039](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L1017-L1039) — 这段做三件事：①`parse_color_code` 支持 `#`、`0x`/`0X` 前缀，并展开 `#fff`→`ffffff`、`#ffff`→`ffffffff` 这种 CSS 风格简写；②6 位 hex 补 `0xff000000`（默认全不透明），8 位 hex 原样保留 alpha；③按 `fmt` 选择是否调用 `ARGB2ABGR`/`RGBA2ABGR`，最后 `& 0xffffffff` 截断。

`label_color` 这类「没填就给个合理默认值」的字段，用 `blend_colors` 做带 alpha 的前后景混合：

[RimeWithWeasel/RimeWithWeasel.cpp:L937-L968](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L937-L968) — 标准的「over」合成算子。先按 alpha 算出合成后的总不透明度，再对各通道做加权平均：

\[
\alpha_{out} = \alpha_f + (1 - \alpha_f)\,\alpha_b, \qquad
C_{out} = \frac{C_f\,\alpha_f + C_b\,\alpha_b\,(1 - \alpha_f)}{\alpha_{out}}
\]

结果仍以 ABGR（COLORREF）返回。

真正把「yaml 颜色键 → `UIStyle` 字段」整张表串起来的是 `_UpdateUIStyleColor`，它用一个 `COLOR` 宏把每个颜色键、目标字段、缺省回退值列成一张表：

[RimeWithWeasel/RimeWithWeasel.cpp:L1381-L1413](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L1381-L1413) — 这段是本讲的「颜色字典」。`COLOR("back_color", style.back_color, 0xffffffff)` 的含义是：读 yaml 里 `<配色名>/back_color`，存进 `style.back_color`，若 yaml 没给则用 `0xffffffff`（不透明白）。注意回退值大量引用前一个字段（如 `candidate_text_color` 缺省回退 `text_color`），形成一条「继承链」。`color_format` 在表读取之前先被解析：

[RimeWithWeasel/RimeWithWeasel.cpp:L1376-L1382](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L1376-L1382) — 默认 `COLOR_ABGR`，若 yaml 写了 `color_format: "argb"` / `"rgba"` 则改写 `fmt`，之后所有 `COLOR(...)` 调用都用这个 `fmt` 解析。

#### 4.2.4 代码实践

**实践目标**：手算一个 ARGB 颜色经过 `ARGB2ABGR` 后的结果，验证你对字节重排的理解。

**操作步骤**：

1. 假设 yaml 里写 `color_format: argb`，某字段值 `0xFFFF8000`（ARGB：A=FF, R=FF, G=80, B=00，即不透明橙色）。
2. 套用本节的 ARGB2ABGR 公式，逐字节重排：A 不动、R↔B 交换。
3. 写出转换后的 ABGR 值。

**预期结果**：ARGB `0xFFFF8000`（A=FF R=FF G=80 B=00）→ ABGR `0xFF0080FF`（A=FF B=00 G=80 R=FF）。手算后，把这两个值分别代入 `GetRValue/GetGValue/GetBValue` 验证：ABGR 形式下 `GetRValue(0xFF0080FF)=0xFF`（红=255），`GetBValue(0xFF0080FF)=0x00`（蓝=0），与「橙色」一致；若不转换、直接把 `0xFFFF8000` 当 ABGR，`GetRValue` 会得到 `0x00`，颜色就全错了。

**延伸验证（源码阅读型）**：打开 [WeaselUI/WeaselPanel.cpp:L1301-L1309](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L1301-L1309)，确认 `_TextOut` 正是用 `GetRValue/GetGValue/GetBValue` 取通道、用 `(inColor >> 24) & 255` 取 alpha。这反过来说明：存储布局必须是 ABGR，否则这两个宏取错通道。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `aqua` 配色里 `text_color: 0x000000` 不需要写 `color_format`？

> **答案**：因为它用的是 Rime 历史默认的 ABGR/BGR 风格（`0xBBGGRR`），与代码默认值 `COLOR_ABGR` 一致，无需转换。只有当用户想用 `#RRGGBB` 这种 ARGB 写法时，才需要显式 `color_format: argb`。

**练习 2**：6 位 hex `0x123456` 解析后存进 `UIStyle` 的值是多少？

> **答案**：6 位补全 alpha → `0xFF123456`（ABGR，alpha=FF 不透明）。按 ABGR 解释：B=0x12, G=0x34, R=0x56。

**练习 3**：`_RimeGetColor` 同时支持 `0x` 前缀和 `#` 前缀，还支持 `#fff` 三位简写。这套灵活解析带来了什么风险？

> **答案**：解析器很宽容，意味着写错格式（比如漏写前缀、位数不对）不会报错，而是走 `fallback` 缺省值，颜色「悄悄」变成默认色而不抛异常。调试配色时若颜色没生效，第一反应应是检查拼写与前缀，而不是怀疑绘制代码。

---

### 4.3 weasel.custom.yaml 定制与刷新链路

#### 4.3.1 概念说明

前两节讲了「字段长什么样」和「颜色怎么解析」，本节回答最实际的工程问题：**我改了一份配色，它是怎么一路生效到屏幕上的？**

这条链路涉及四个进程/组件协作：

1. **WeaselDeployer**（用户设定 UI）：把用户在「UI 配色」对话框选的配色方案写进 `weasel.custom.yaml`（覆盖层）。
2. **WeaselServer**（librime）：重启或重新部署后，`config_open("weasel")` 把 `weasel.yaml` 与 `weasel.custom.yaml` 合并读出，经 `_UpdateUIStyle` / `_UpdateUIStyleColor` 填进 `UIStyle`。
3. **IPC 传输**：某次按键响应里，`_Respond` 把整份 `UIStyle` 用 `boost` 序列化成 `style=...` 一行，写回管道。
4. **WeaselUI**（前端 DirectWriteResources）：前端 `ResponseParser` 的 `Styler` 反序列化 `style=`，更新本端 `UIStyle`，触发重绘。

其中 1 属于「写配置」，2~4 属于「读配置并应用」。理解的关键是「覆盖层」与「`__synced` 惰性重发」两个机制。

#### 4.3.2 核心流程

完整链路时序：

```text
[用户设定] WeaselDeployer「UI 配色」对话框
     │  SelectColorScheme("my_scheme")
     │  → customize_string("style/color_scheme", ...) 写 __patch 进 weasel.custom.yaml
     ▼
[提示重新部署] 用户点「重新部署」/ 重启 WeaselServer
     │
     ▼
[Server 启动] Initialize()
     │  config_open("weasel")  ← 自动叠加 weasel.custom.yaml 的 __patch
     │  _UpdateUIStyle()       ← 读 style/font/layout
     │  若 dark → _UpdateUIStyleColor(color_scheme_dark)
     │  m_base_style = m_ui->style()
     ▼
[建会话] AddSession()
     │  session_status.style = m_base_style
     │  _LoadSchemaSpecificSettings() ← 叠加方案专属配色
     │  __synced = false   ← 标记「样式需要重发给前端」
     ▼
[按键响应] _Respond()
     │  if (!__synced) { boost 序列化 style → "style=...\n"; __synced=true }
     │  eat(...) 写回管道
     ▼
[前端] ResponseParser::Feed("style=...")
     │  Styler 反序列化 → 本端 UIStyle 更新
     ▼
[重绘] WeaselPanel::DoPaint
        _HighlightText / _TextOut 用新的 *_color、round_corner、shadow_radius 画
```

两个关键设计：

- **覆盖层 `__patch`**：`weasel.custom.yaml` 不重写整份 `weasel.yaml`，而是用 `__patch: { "style/color_scheme": "my_scheme" }` 只覆盖需要的键。librime 的 levers 模块负责合并（见 u6-l1）。
- **`__synced` 惰性重发**：样式是「大块」数据，每次按键都重发很浪费。所以 `__synced` 只在「样式真变了」（切方案、切深色模式、建新会话）时置 `false`，下一次 `_Respond` 才重发一次，发完立即置 `true`。

#### 4.3.3 源码精读

**写配置（Deployer 侧）**：`UIStyleSettings::SelectColorScheme` 把选中的配色 id 写进 `style/color_scheme`：

[WeaselDeployer/UIStyleSettings.cpp:L71-L75](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp#L71-L75) — `customize_string` 是 levers 覆盖层的写入接口，效果是往 `weasel.custom.yaml` 追加一条 `__patch` 指令，把 `style/color_scheme` 改成用户选的配色 id。

下拉框里列出的可选项来自 `GetPresetColorSchemes`，它遍历 `preset_color_schemes` 这个 map：

[WeaselDeployer/UIStyleSettings.cpp:L10-L39](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettings.cpp#L10-L39) — 用 `config_begin_map("preset_color_schemes")` 枚举每个配色，取出 `name`/`author`/`key(id)` 填进 `ColorSchemeInfo`。这就是「UI 配色」对话框下拉框的数据源。

对话框里改选配色时触发回写：

[WeaselDeployer/UIStyleSettingsDialog.cpp:L57-L64](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselDeployer/UIStyleSettingsDialog.cpp#L57-L64) — `OnColorSchemeSelChange` 取当前选中项的 `color_scheme_id`，调 `SelectColorScheme` 写覆盖层，再 `Preview` 刷新预览图。

**读配置（Server 侧）**：`Initialize` 里读 `weasel.yaml`（含覆盖层）算出基础样式，深色模式再叠加 `color_scheme_dark`：

[RimeWithWeasel/RimeWithWeasel.cpp:L120-L136](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L120-L136) — `_UpdateUIStyle(&config, m_ui, true)` 读全部样式字段；`IsUserDarkMode()` 为真且配置了 `color_scheme_dark` 时，再调 `_UpdateUIStyleColor` 覆盖配色；结果存为 `m_base_style`，是所有会话的样式基线。

`_LoadSchemaSpecificSettings` 在切方案时叠加「方案专属配色」，并按当前明暗模式选择 `color_scheme` 还是 `color_scheme_dark`：

[RimeWithWeasel/RimeWithWeasel.cpp:L587-L590](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L587-L590) — `key = m_current_dark_mode ? "style/color_scheme_dark" : "style/color_scheme"`，这就是「同一份配置在深浅色下走不同配色」的切换点。配色表先在方案文件里找，找不到再退回 `weasel` 全局配置（见 [L572-L585](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L572-L585)）。

运行期切深色模式由 `UpdateColorTheme` 驱动，它会为每个会话重算样式并置 `__synced = false`：

[RimeWithWeasel/RimeWithWeasel.cpp:L230-L262](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L230-L262) — 注意循环末尾 `pair.second.__synced = false`，这正是「主题一变，下次按键就把新配色整块重发给前端」的扳机。

**IPC 传输**：`_Respond` 里的 `__synced` 惰性序列化就是 4.1.3 见到的那段：

[RimeWithWeasel/RimeWithWeasel.cpp:L903-L911](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L903-L911) — `text_woarchive` 把 `session_status.style` 整块序列化成宽字符串，作为 `style=` 行发出去；发完立即 `__synced = true`，避免下次重复发送。这与前端 `Styler` 的 `text_wiarchive` 反序列化严格配对（见 u2-l5、u7-l1）。

**绘制应用（前端）**：颜色字段被两处消费。文字色经 `_TextOut` 转 DirectWrite 画刷（见 4.2.4）。背景/阴影/边框经 `_HighlightText`，用 GDI+ 画圆角矩形路径，其中阴影只在 `shadow_radius > 0` 且颜色非透明时才画：

[WeaselUI/WeaselPanel.cpp:L557-L563](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L557-L563) — `DPI_SCALE(shadow_radius)` 与 `COLORNOTTRANSPARENT(shadowColor)` 两个条件同时成立才进入阴影绘制分支，呼应 `aqua` 里 `shadow_color: 0x00000000` 因 alpha=0 被跳过的现象。

透明度判定宏与 COLORREF→GDI+ 转换宏定义在文件头：

[WeaselUI/WeaselPanel.cpp:L17-L24](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L17-L24) — `COLORTRANSPARENT` 看 alpha 字节是否为 0；`GDPCOLOR_FROM_COLORREF` 用 `MakeARGB(a, GetRValue, GetGValue, GetBValue)` 把内部 ABGR 重新拼成 GDI+ 需要的 ARGB（按通道名拼装，所以不会取错）。`HALF_ALPHA_COLOR` 把高亮背景的 alpha 减半，用于按压反馈动画。

> 颜色格式转换在「写进 `UIStyle` 之前」就完成了（`_UpdateUIStyleColor` 里），所以前端拿到的 `style.*_color` 全是 ABGR，`_TextOut` / `_HighlightText` 直接用 `GetRValue` 等宏即可——这是「转换只做一次、消费端零感知」的设计。

#### 4.3.4 代码实践

**实践目标**：写一份自定义 `weasel.custom.yaml`，定义一套含颜色、圆角、阴影、字体的配色，并解释它生效的全过程。这是本讲的核心实战。

**操作步骤**（在 Windows 安装了 Weasel 的机器上进行；本环境为 Linux，仅给出配置与预测，**像素效果待本地验证**）：

1. 在用户数据目录（通常是 `%AppData%\Rime\`）新建/编辑 `weasel.custom.yaml`。
2. 写入下面这份「示例配置」（注释为本讲所加，不是项目原有内容）：

```yaml
# 示例代码：weasel.custom.yaml —— 自定义配色「my_theme」
patch:
  # 1) 切换到自定义配色
  "style/color_scheme": my_theme
  # （可选）深色模式下用另一套
  "style/color_scheme_dark": my_theme_dark

  # 2) 字体与字号
  "style/font_face": "Microsoft YaHei"
  "style/label_font_face": "Consolas"
  "style/font_point": 16

  # 3) 圆角与阴影（外观字段）
  "style/layout/round_corner": 8         # 高亮候选圆角
  "style/layout/corner_radius": 6        # 普通候选/背景圆角
  "style/layout/shadow_radius": 4        # 阴影模糊半径 >0 才画阴影
  "style/layout/shadow_offset_x": 2
  "style/layout/shadow_offset_y": 2
  "style/layout/border_width": 1

  # 4) 定义配色本身（ABGR，与 Rime 默认一致）
  "preset_color_schemes/my_theme":
    name: 我的主题／My Theme
    author: me
    color_format: abgr          # 显式声明，默认也是 abgr
    back_color: 0xFF282828       # 候选窗背景：不透明深灰
    border_color: 0xFF555555
    text_color: 0xFFEEEEEE       # 编码行文字
    hilited_back_color: 0xFF335577
    hilited_candidate_text_color: 0xFFFFFFFF
    hilited_candidate_back_color: 0xFF1F6FEB   # 高亮候选背景：蓝
    hilited_candidate_shadow_color: 0x66000000 # 半透明黑阴影
    candidate_text_color: 0xFFCCCCCC
    label_text_color: 0xFF888888
    comment_text_color: 0xFF888888
```

3. 通过托盘菜单「用户设定」→「重新部署」（或重启 WeaselServer）让它生效。

**需要观察的现象 / 预期结果**（基于源码逻辑的预测）：

- `style/color_scheme: my_theme` 经 `SelectColorScheme`/`customize_string` 写成 `__patch`，合并进 `weasel.yaml`。
- `Initialize` → `_UpdateUIStyle` 读到 `font_point=16`、`round_corner=8`、`shadow_radius=4`；`_UpdateUIStyleColor` 按表读 `my_theme` 的各颜色。`color_format: abgr` 命中默认值，不做字节转换；`0xFF282828` 因是 8 位写法，alpha 原样保留为 `0xFF`（不透明）。
- `hilited_candidate_shadow_color: 0x66000000` 的 alpha=`0x66`（约 40%），非零 → `_HighlightText` 会为高亮候选画一层半透明阴影。
- 切方案或建会话后 `__synced=false`，下一次按键 `_Respond` 把整份 `UIStyle` 序列化成 `style=...` 发往前端；前端 `Styler` 反序列化更新本端样式，`WeaselPanel::DoPaint` 重绘。
- 因为 `shadow_radius > 0` 且高亮阴影色非透明，应当能看到高亮候选背后有一圈柔和阴影（GdiplusBlur 盒滤波近似高斯，见 u5-l3）。

> 若阴影没出现，按 4.2.5 练习 3 的思路排查：先确认 `shadow_radius>0`、阴影色 alpha 非 0、布局不是全屏（全屏布局会强制关阴影，见 [RimeWithWeasel/RimeWithWeasel.cpp:L1295-L1298](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L1295-L1298)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么改完 `weasel.custom.yaml` 后通常需要「重新部署」或重启 WeaselServer，而不是立即生效？

> **答案**：`weasel.yaml`（含覆盖层）在 `Initialize` 时由 `config_open("weasel")` 读入并算成 `m_base_style`，此后驻留内存。yaml 文件改了不会自动重读，必须让 Server 重新走初始化（重新部署或重启）才会重算样式。

**练习 2**：如果同一字段既在 `weasel.yaml` 里、又在 `weasel.custom.yaml` 的 `patch:` 里，最终用哪个？

> **答案**：用 `weasel.custom.yaml` 的值。levers 覆盖层的设计就是让用户的 `__patch` 叠加（覆盖）在发行版默认值之上，用户层优先（见 u6-l1）。

**练习 3**：`__synced` 机制为什么不能改成「每次按键都重发 style」？

> **答案**：`UIStyle` 有约 80 个字段，整块 boost 序列化是一段不小的文本。每次按键都发会浪费管道带宽、增加延迟，而样式在绝大多数按键间是稳定的。`__synced` 只在样式真变化时置 `false`、下一次响应重发一次，兼顾了「及时生效」与「低开销」。

---

## 5. 综合实践

把本讲三节串成一个完整任务：**为 Weasel 新增一个自定义深色配色，并在不重启的情况下观察它何时被前端拿到**。

1. **设计配色**：参考 [output/data/weasel.yaml:L565-L578](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/output/data/weasel.yaml#L565-L578) 的 `psionics`（深灰底、金色高亮），用 ARGB 写法（`color_format: argb`）重新等价表达一遍，验证你对 4.2 字节转换的掌握。例如 `psionics` 的 `back_color: 0x444444`（ABGR/6位）写成 ARGB 6 位应是 `0x444444`（因为 R=G=B，交换无差别）；但 `hilited_candidate_back_color: 0xd8bf00`（ABGR 即 B=d8 G=bf R=00）转成等价 ARGB 写法应是多少？请手算并在配置里验证。
2. **写覆盖层**：把这份配色写进 `weasel.custom.yaml` 的 `preset_color_schemes/`，并 `patch` 到 `style/color_scheme_dark`。
3. **追踪链路**：在日志（`%TEMP%\rime.weasel\rime.weasel.*.log`）开启 `DLOG` 后，切换系统深色模式，观察是否触发 `UpdateColorTheme` → 各会话 `__synced=false` → 下一次按键响应的 `style=` 行重发。若日志不易获取，改用「源码阅读」方式：在 [RimeWithWeasel/RimeWithWeasel.cpp:L250-L258](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel.cpp#L250-L258) 处确认循环会遍历所有会话并重算样式。
4. **绘制验证**：解释为什么改 `shadow_radius` 会影响窗口「捕获鼠标」的范围（提示：见 [WeaselUI/WeaselPanel.cpp:L359-L360](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselUI/WeaselPanel.cpp#L359-L360) 附近 `shadow_color` 透明时缩小捕获矩形的逻辑）。

> 手算提示：`0xd8bf00` 作为 ABGR 是 B=d8, G=bf, R=00；等价的 ARGB（A,R,G,B）6 位写法是 `0x00bfd8`（R=00,G=bf,B=d8）。在 `color_format: argb` 下写 `hilited_candidate_back_color: 0x00bfd8` 应得到相同像素效果——**待本地验证**。

## 6. 本讲小结

- `UIStyle` 用 23 个 `int` 颜色字段 + 一组圆角/阴影/字体几何字段，构成「服务端决策、前端执行」的样式契约；字段名遵循「对象 + `hilited_` 状态」命名规律。
- Weasel 内部统一用 `0xAABBGGRR`（`COLOR_ABGR`）布局，因为绘制代码依赖 Windows 的 `GetRValue/GetBValue`（COLORREF 是 `0x00BBGGRR`）。用户写 ARGB/RGBA 时由 `ARGB2ABGR`/`RGBA2ABGR` 做字节重排转换。
- `_RimeGetColor` 是宽容的解析器：支持 `#`/`0x` 前缀、3/4 位简写、6 位补全 alpha、8 位保留 alpha；写错不报错而走缺省值，调试配色时需先排查拼写。
- `_UpdateUIStyleColor` 用 `COLOR(键, 字段, 回退)` 宏表把 yaml 颜色键映射到 `UIStyle` 字段，回退值形成继承链（如 `candidate_text_color` 缺省回退 `text_color`）。
- 配色定制走 levers 覆盖层：Deployer 用 `customize_string` 把选择写进 `weasel.custom.yaml` 的 `__patch`，合并进 `weasel.yaml` 后由 `Initialize`/`_LoadSchemaSpecificSettings` 读入。
- `__synced` 惰性重发让大块样式只在「切方案/切深色/建会话」时整块序列化重发一次，兼顾及时生效与低开销；前端 `Styler` 反序列化后由 `WeaselPanel` 的 `_TextOut`/`_HighlightText` 应用到 DirectWrite/GDI+ 绘制。

## 7. 下一步学习建议

- **深入序列化往返**：阅读 u7-l1 的测试工程，动手用「构造 `UIStyle` → `text_woarchive` 序列化 → `Styler` 的 `text_wiarchive` 反序列化」的往返方式为某个颜色字段写一个单元测试，体会 `style=` 这一行二进制文本的脆弱性与兼容约束。
- **扩展点视角**：若想新增一个颜色字段（例如 `hilited_candidate_label_shadow_color`），按本讲梳理需要同步改动四处——`UIStyle` 字段声明、`operator!=`、`serialize` 模板、`_UpdateUIStyleColor` 的 `COLOR` 表，正好印证 u7-l4（扩展点与架构权衡）的总结。
- **布局与绘制**：颜色字段只是「填什么色」，至于「画成什么形状、矩形在哪」由布局系统决定。继续阅读 u5-l2（Layout 多态）与 u5-l3（DirectWrite 资源与分层绘制），把「配色 → 布局 → 绘制」三层彻底打通。
