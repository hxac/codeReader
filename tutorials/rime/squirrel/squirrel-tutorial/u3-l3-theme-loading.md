# SquirrelTheme 主题加载

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `SquirrelTheme.load(config:dark:)` 的「全局 `style/*` → 配色方案叠加覆盖」两层加载结构，以及它们之间的覆盖优先级。
- 解释 `color_scheme` / `color_scheme_dark` / `preset_color_schemes` 三者的关系，并列举配色方案可以覆盖哪些「全局样式项」。
- 理解 `decodeFonts` 如何把逗号分隔的字体串解析为字体数组，`combineFonts` 又如何用 `cascadeList` 把多套字体级联成单个 `NSFont`。
- 说明 `candidate_format` 中旧式占位符 `%@` 与 `%c` 各自被归一化成什么具名占位符，以及为什么要做这层归一化。

本讲是第三单元（配置与主题）的第三篇，承接 u3-l1（`SquirrelConfig` 类型化配置门面）与 u3-l2（`squirrel.yaml` 文件结构），把视线从「配置如何被读取」推进到「配置如何被组装成一个可绘制的外观主题对象」。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个本讲反复出现的概念。

**主题（Theme）是什么。** 输入法候选面板要画出来，需要一堆「外观参数」：背景什么颜色、文字什么颜色、圆角多大、用什么字体、候选项怎么排版……这一整套参数的集合就是「主题」。在 Squirrel 里它对应 `SquirrelTheme` 类。`SquirrelConfig` 负责「从 YAML 读出一个值」，`SquirrelTheme` 负责「把这些值组装成 AppKit 能直接用的 `NSColor` / `NSFont` / 富文本属性字典」。

**两层配置：全局 style 与配色方案。** `squirrel.yaml` 里和外观相关的配置分两层。第一层是 `style:` 节下的「全局样式项」，比如 `corner_radius`、`font_face`、`candidate_format`。第二层是 `preset_color_schemes:` 仓库里预先定义好的一套套「配色方案」（如 `aqua`、`luna`、`solarized_light`），每套方案主要定义颜色，但**也允许重复定义那些全局样式项**。`style/color_scheme` 指向某个方案名后，该方案里的同名项就会**覆盖**全局 `style/*` 里的值。这正是本讲的核心机制。

**`native` 配色。** 当 `color_scheme` 设为 `native`（默认值），表示「不指定具体颜色，跟随系统窗口外观（亮/暗）的语义色」。源码里用 `native` 布尔标志区分这种情况。

**色空间（Color Space）。** 同一组 RGB 数值在不同色空间下显示效果不同。Rime 的颜色串（如 `0xeefa3a0a`）需要声明它属于 `display_p3` 还是 `sRGB`，Squirrel 才能正确还原。本讲会看到 `color_space` 字段如何决定 `NSColor` 的构造方式。

**`?=` 运算符。** 这是 Squirrel 项目自定义的「可选赋值」运算符：`a ?= b` 表示「仅当 `b` 非 nil 时才把 `b` 赋给 `a`」。它在 u3-l1、u2-l3 已多次出现，本讲是它最密集的用武之地——用来优雅地实现「配色方案有就用配色方案的，没有就保留全局的」覆盖语义。

## 3. 本讲源码地图

本讲主要涉及以下文件：

| 文件 | 作用 |
| --- | --- |
| [sources/SquirrelTheme.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift) | 主题类本体，`load` 方法与字体/颜色处理逻辑都在这里，是本讲的绝对主角。 |
| [sources/SquirrelConfig.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift) | 类型化配置门面，`getString`/`getDouble`/`getBool`/`getColor` 是 `load` 读取配置的底层工具。 |
| [data/squirrel.yaml](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml) | 默认配置文件，`style` 节与 `preset_color_schemes` 仓库的真实样例。 |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | `loadSettings` 与 `loadSettings(for:)` 在此处调用面板加载，是主题加载的入口。 |
| [sources/SquirrelPanel.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift) | `SquirrelPanel.load` 创建亮/暗两个 `SquirrelTheme` 并调用 `theme.load`，消费 `candidateFormat`。 |
| [sources/BridgingFunctions.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift) | 定义 `?=` 运算符。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块拆分：全局 `style/*` 读取、配色方案叠加覆盖、字体解析与级联、`candidate_format` 归一化。它们都集中在 `SquirrelTheme.load(config:dark:)` 这一个方法及其辅助函数里。

### 4.1 全局 style/* 读取

#### 4.1.1 概念说明

`load` 方法的第一层职责，是先把 `squirrel.yaml` 里 `style:` 节下的「全局样式项」读进来，填到 `SquirrelTheme` 的各个属性上。这一层是「基线（baseline）」：无论最终用不用配色方案覆盖，这些值都先作为默认外观存在。

这里有个关键设计：所有读取都用 `?=` 运算符。因为 `config.getString` 这类方法返回的是 `Optional`（键不存在就返回 nil，这是 u3-l1 讲过的门面约定），而主题属性大多是非可选类型且有默认值。`?=` 让我们用一行代码表达「配置里有就用配置里的，没有就保持默认值」。

#### 4.1.2 核心流程

`load` 读取全局项的流程可以概括为：

1. 读「布尔型布局/行为开关」（如 `candidate_list_layout`、`text_orientation`、`inline_preedit`）。
2. 读「数值型几何参数」（如 `corner_radius`、`line_spacing`、`alpha`）。
3. 读「字体名与字号」（`font_face`、`font_point` 及 label/comment 变体），先存进局部变量。
4. 根据 `dark` 参数决定读 `style/color_scheme` 还是 `style/color_scheme_dark`，进入配色方案分支（4.2 讲）。
5. 最后用 `decodeFonts` 把字体名解析成字体数组。

布尔开关里有两类需要特别留意：`candidate_list_layout` 和 `text_orientation` 在 YAML 里是字符串（`"linear"`/`"stacked"`、`"horizontal"`/`"vertical"`），读进来后用 `.map { $0 == "linear" }` 转成布尔值 `linear`、`vertical`。而 `alpha` 会被 `min(1, max(0, $0))` 钳制到 `[0, 1]` 区间，`shadowSize` 被 `max(0, $0)` 钳到非负——这是对配置脏数据的防御。

#### 4.1.3 源码精读

下面是 `load` 方法读全局 `style/*` 的核心段落：

[sources/SquirrelTheme.swift:197-226](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L197-L226) 这段是 `load` 的开头：先读布局与行为布尔开关（`linear`/`vertical`/`inlinePreedit`/`translucency` 等），再读 `candidate_format` 与全部几何数值，最后把字体名/字号读到局部变量 `fontName`/`fontSize` 等。注意 `linear`、`vertical` 是用 `.map { $0 == "linear" }` 把字符串归一成布尔的。

支撑这套写法的 `?=` 运算符定义在桥接文件里：

[sources/BridgingFunctions.swift:44-56](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L44-L56) 这里定义了 `?=` 运算符的两个重载：一个作用于非可选左值 `inout T`，一个作用于可选左值 `inout T?`。两者都遵循「右值非 nil 才赋值」的语义，优先级是 `AssignmentPrecedence`。

YAML 里这些全局项的真实样貌：

[data/squirrel.yaml:27-74](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L27-L74) 这是 `style:` 节，可以看到 `color_scheme: native`（第 28 行）、`candidate_list_layout: stacked`、`corner_radius: 7`、`font_face: 'Avenir'`、`font_point: 16`、`candidate_format` 等全局项，正是 `load` 第一层读取的目标。

#### 4.1.4 代码实践

**实践目标：** 亲手验证「全局 style 项 → 主题属性」的映射关系。

**操作步骤：**

1. 打开 [data/squirrel.yaml](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml) 的 `style:` 节，挑一个数值项，例如 `corner_radius: 7`。
2. 打开 [sources/SquirrelTheme.swift:211](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L211)，确认它对应 `cornerRadius ?= config.getDouble("style/corner_radius")`。
3. 在浏览器里把 YAML 的键名与源码里的配置路径（`"style/corner_radius"`）逐一对照，做一张映射表。

**需要观察的现象：** 每一个 YAML 键都能在源码里找到对应的 `config.getXxx("style/<键名>")` 调用；反之，源码里读的每一个 `style/*` 路径都能在 YAML 里找到（或注释掉的）同名键。

**预期结果：** 你会发现字符串型布局项（`candidate_list_layout`/`text_orientation`）在源码里都带 `.map { $0 == "..." }`，而纯数值项直接用 `getDouble`。这印证了 4.1.2 提到的「字符串归一为布尔」设计。

（待本地验证：若你已按 u1-l3 构建出 Squirrel.app，可把 `~/Library/Rime/squirrel.yaml` 的 `corner_radius` 改成一个夸张值（如 30），执行 `Squirrel --reload` 重新部署后观察候选面板圆角变化。）

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `alpha ?=` 后面还要跟一个 `min(1, max(0, $0))`？如果用户在 YAML 里写了 `alpha: 1.5` 会怎样？

**参考答案：** `alpha` 表示不透明度，物理上只对 `[0, 1]` 有意义。用户可能误写超范围值，`min(1, max(0, $0))` 把它钳制到 `[0, 1]`，所以 `1.5` 会被当成 `1.0`（完全不透明）。`shadowSize` 同理被 `max(0, $0)` 钳到非负。

**练习 2：** `candidate_list_layout: stacked` 最终会让 `SquirrelTheme` 的哪个属性变成什么值？

**参考答案：** `linear ?= config.getString("style/candidate_list_layout").map { $0 == "linear" }`。`"stacked" != "linear"`，所以 `.map` 返回 `false`，`linear` 被赋为 `false`（即「非线性的堆叠排列」）。

### 4.2 color_scheme / preset_color_schemes 叠加覆盖

#### 4.2.1 概念说明

读完全局基线后，`load` 进入第二层：配色方案叠加覆盖。这一层是本讲最核心的机制。

思路是这样的：全局 `style/*` 给了一套默认外观，但很多时候我们希望「换一套配色就换一种风格」，而且换的时候可能连圆角、字体、排版都想一起改。于是 Squirrel 允许每个配色方案（`preset_color_schemes/<方案名>`）不仅定义颜色，还能**重新定义那些全局样式项**。当 `style/color_scheme` 指向某方案后，该方案里出现的同名项就覆盖掉第一层读到的基线值。

`dark` 参数决定走亮色还是暗色方案：亮色读 `style/color_scheme`，暗色读 `style/color_scheme_dark`，二者可分别指向不同方案（实现跟随系统亮暗切换，详见 u3-l4）。

#### 4.2.2 核心流程

配色方案分支的流程：

1. 根据 `dark` 选键名：`colorSchemeOption = dark ? "style/color_scheme_dark" : "style/color_scheme"`。
2. 读出方案名 `colorScheme`。若该键不存在，说明没有任何配色方案 → `available = false`，整段跳过（保留全局基线）。
3. 若方案名是 `"native"`：不做任何覆盖，`native` 标志保持默认 `true`，表示用系统语义色。
4. 否则 `native = false`，构造前缀 `prefix = "preset_color_schemes/<方案名>"`，然后：
   - 读 `color_space` 决定色空间；
   - 读所有颜色项（背景、文字、高亮、边框、注释等十几个）；
   - **重新读一遍全局样式项**（布局、几何、字体、`candidate_format`），只是这次路径前缀换成 `<prefix>/`，从而实现覆盖。
5. 若配色方案里某些颜色缺失，则回退到相关的「主色」（如 `hilited_candidate_back_color` 缺失则回退到 `hilited_back_color`），保证外观基本可用。

覆盖语义之所以成立，全靠 `?=`：「配色方案里这一项有值（非 nil）才覆盖，否则保留第一层读到的全局值」。

#### 4.2.3 源码精读

[sources/SquirrelTheme.swift:228-281](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L228-L281) 这是配色方案分支的主体。第 228 行根据 `dark` 选键；第 229 行读方案名；第 230 行判断 `!= "native"`；第 232 行构造 `prefix`；第 233 行读 `color_space`；第 234-250 行读所有颜色（注意第 236 行 `hilited_candidate_back_color` 用 `??` 回退到 `hilited_back_color`，第 242-244 行文字色也层层回退）；第 252-277 行是关键的「覆盖全局项」——把第一层读过的 `linear`/`vertical`/`cornerRadius`/`fontName` 等全部用 `prefix/` 前缀重读一遍；第 279-281 行的 `else` 分支在方案不存在时置 `available = false`。

注意第 230 行 `if colorScheme != "native"` 的分支结构与第 279 行的 `else`：`else` 对应的是「`colorScheme` 为 nil（即 YAML 没设 color_scheme）」的情况，而非 `native`。`native` 走的是「键存在但值是 native」→ 跳过覆盖、`native` 保持 true。

色空间的枚举与解析：

[sources/SquirrelTheme.swift:19-28](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L19-L28) `RimeColorSpace` 是个两值枚举（`displayP3` / `sRGB`），`from(name:)` 把字符串 `"display_p3"` 映射成 `.displayP3`，其余一律按 `sRGB` 处理。

颜色最终如何变成 `NSColor`：

[sources/SquirrelConfig.swift:122-147](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L122-L147) `color(from:inSpace:)` 用正则解析 `0xAABBGGRR`（8 位）或 `0xBBGGRR`（6 位，Alpha 默认 255）格式的颜色串，按 Rime 历史字节序拆出 alpha/blue/green/red（注意正则捕获组顺序是 `alpha, blue, green, red`，这是 Rime 约定的反字节序，u3-l1 已讲过）。然后 `color(alpha:red:green:blue:colorSpace:)` 按色空间分支构造 `NSColor(displayP3Red:...)` 或 `NSColor(srgbRed:...)`。

YAML 里的配色方案仓库样例：

[data/squirrel.yaml:76-90](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L76-L90) 这是 `preset_color_schemes:` 开头与 `native`、`aqua` 两个方案。注意第 77-78 行 `native` 方案只有一个 `name:`，没有任何颜色——它靠 `load` 里的 `native == true` 路径用系统语义色。`aqua` 方案则定义了 `text_color`/`back_color`/`hilited_candidate_back_color` 等一串颜色。

一个「配色方案覆盖全局项」的真实样例：

[data/squirrel.yaml:235-253](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L235-L253) `clean_white` 方案里不仅有颜色，还重新定义了 `candidate_list_layout: linear`、`candidate_format: '%c %@'`、`corner_radius: 6`、`border_height`/`border_width`、`font_point`、`label_font_point`——这些都是「全局 style 项」在配色方案里的覆盖。这正是 4.2.1 所说的「换配色连带改排版」。

#### 4.2.4 代码实践

**实践目标：** 列出配色方案可以覆盖的「全局 style 项」，验证覆盖优先级。

**操作步骤：**

1. 打开 [sources/SquirrelTheme.swift:252-277](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L252-L277)，把这段重读的键名（去掉 `\(prefix)/` 前缀后）逐条抄下，与 4.1 里第一层读的键名对比。
2. 打开 [data/squirrel.yaml](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml)，找出 `clean_white`、`apathy`、`dust`、`mojave_dark` 这几个方案各自覆盖了哪些全局项。

**需要观察的现象：** 源码第 252-277 行重读的键，应当与第 198-226 行第一层读的键**完全一一对应**（布局、行为、几何、字体、`candidate_format`），只是路径前缀不同。YAML 里某些方案（如 `apathy`）覆盖了 `candidate_list_layout`、`inline_preedit`、`candidate_format`、`corner_radius`、`font_face` 等，而另一些方案（如 `aqua`）只定义颜色、不覆盖任何全局项。

**预期结果：** 至少能列出以下 6 个（实际更多）可被配色方案覆盖的全局项：`candidate_list_layout`、`text_orientation`、`inline_preedit`、`inline_candidate`、`translucency`、`mutual_exclusive`、`show_paging`、`candidate_format`、`font_face`、`font_point`、`label_font_face`、`label_font_point`、`comment_font_face`、`comment_font_point`、`alpha`、`corner_radius`、`hilited_corner_radius`、`surrounding_extra_expansion`、`border_height`、`border_width`、`line_spacing`、`spacing`、`base_offset`、`shadow_size`。覆盖关系为：**方案里有该键 → 覆盖全局；没有 → 保留全局基线**（由 `?=` 保证）。

（待本地验证：在 `~/Library/Rime/squirrel.yaml` 里把 `style/color_scheme` 从 `native` 改成 `clean_white`，部署后候选词会从「堆叠」变成「横向线性」排列——因为 `clean_white` 用 `candidate_list_layout: linear` 覆盖了全局的 `stacked`。）

#### 4.2.5 小练习与答案

**练习 1：** 如果 YAML 里既没写 `style/color_scheme`，也没写 `style/color_scheme_dark`，`load` 走哪条路？`available` 和 `native` 分别是什么？

**参考答案：** `config.getString(colorSchemeOption)` 返回 nil，进入第 279 行的 `else` 分支，`available = false`；`native` 保持初始默认值 `true`（因为没进入 `native = false` 的赋值）。这表示「没有可用配色方案，将用系统语义色与全局基线样式」。

**练习 2：** 为什么 `hilited_candidate_back_color` 缺失时要回退到 `hilited_back_color`，而不是回退到全局基线？

**参考答案：** 因为「高亮候选项背景」与「高亮预编辑背景」视觉上属于同一组高亮色，用一个统一的高亮底色能保证配色协调。回退到 `hilited_back_color`（同一方案内的高亮色）比回退到全局基线更符合「同一套配色内部自洽」的设计。而 `?=` 在这里帮不上忙——`??` 处理的是「同类型默认值」，`?=` 处理的是「是否覆盖」。

### 4.3 decodeFonts 字体解析与 cascadeList 级联

#### 4.3.1 概念说明

`font_face` 这类字段在 YAML 里是一个**字符串**，可能是单个字体名（`'Avenir'`），也可能是逗号分隔的多个字体（`'PingFangSC-Regular,HanaMinB'`）。但 AppKit 绘制富文本需要的是 `NSFont` 对象。从字符串到 `NSFont` 的转换，以及「多套字体如何协同（主字体缺字时回落到备选字体）」，就是本模块要解决的问题。

关键概念是 **cascade list（级联字体表）**：macOS 的字体系统允许给一个主字体挂一组「备选字体」，当主字体画某个字符而它没有该字形（glyph）时，系统会依次到级联表里找能画的字体。这对中日韩输入法尤其重要——拉丁字体通常没有汉字字形，需要级联一个 CJK 字体兜底。

`SquirrelTheme` 里维护三套字体：主字体 `font`、编号字体 `labelFont`、注释字体 `commentFont`，每套都走相同的「解析 + 级联」流程。

#### 4.3.2 核心流程

字体处理分两步：

1. **解析 `decodeFonts`**：把逗号分隔的字符串拆成一个个 `NSFont`。
   - 对每个子串，先用正则 `/^\s*(.+)-([^-]+)\s*$/` 尝试匹配「家族名-字形名」（如 `PingFangSC-Regular` → 家族 `PingFangSC`、字形 `Regular`）。
   - 匹配上就用 `[.family, .face]` 构造字体描述符；匹配不上就把整串当字体名，用 `[.name]` 构造。
   - 用 `seenFontFamilies` 集合去重，避免同家族重复挂载。
   - 任一步构造失败就跳过该项（容错）。
2. **级联 `combineFonts`**：把字体数组合并成单个 `NSFont`。
   - 数组为空 → 返回 nil（调用方回退到默认字体）。
   - 数组只有一个 → 直接用，可按指定字号缩放。
   - 数组有多个 → 把第 0 个当主字体，第 1 个之后的 `fontDescriptor` 组成 `cascadeList` 挂到主字体的描述符上，再用 `NSFont(descriptor:size:)` 重新生成。

最终 `load` 末尾把解析结果存进 `fonts`/`labelFonts`/`commentFonts`，配合字号交给 `lazy var font/labelFont/commentFont`（它们内部调 `combineFonts`）惰性生成真正用于绘制的 `NSFont`。

#### 4.3.3 源码精读

`load` 末尾的字体收尾：

[sources/SquirrelTheme.swift:283-288](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L283-L288) 注意 label/comment 字体名若为 nil 会回退到主字体名（`labelFontName ?? fontName`），所以即便用户只配了 `font_face`，编号与注释也有字体可用。

`decodeFonts` 解析逻辑：

[sources/SquirrelTheme.swift:307-334](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L307-L334) 第 313 行的 `family-style` 正则，第 316 行用 `seenFontFamilies` 去重，第 317-318 行用 `[.family, .face]` 构造，第 326-327 行回退到 `[.name]` 构造。所有字体统一用 `Self.defaultFontSize` 创建，真实字号由后续 `combineFonts` 的 `size` 参数覆盖。

`combineFonts` 级联合并：

[sources/SquirrelTheme.swift:293-305](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L293-L305) 第 302 行把第 1 个之后的字体描述符组成 `cascadeList`，第 303 行 `addingAttributes` 挂到主字体描述符，第 304 行用 `NSFont(descriptor:size:)` 重新实例化——这一步才把「主字体 + 级联表」固化进一个可用的 `NSFont`。

惰性生成最终字体的入口：

[sources/SquirrelTheme.swift:91-109](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L91-L109) `font`/`labelFont`/`commentFont` 都是 `lazy var`，首次访问时调 `combineFonts`，失败则回退到默认字体。`labelFont` 的字号策略是 `labelFontSize ?? fontSize`（编号字号缺省时跟随主字号）。

YAML 里的多字体级联样例：

[data/squirrel.yaml:265](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L265) `apathy` 方案的 `font_face: "PingFangSC-Regular,HanaMinB"` —— 主字体 `PingFangSC-Regular`（拉丁+简体汉字），级联 `HanaMinB`（花園明朝，覆盖 CJK 扩展汉字）。这正是 cascade list 的典型用法。

#### 4.3.4 代码实践

**实践目标：** 追踪一个多字体字符串如何变成带级联表的 `NSFont`。

**操作步骤：**

1. 取字符串 `"PingFangSC-Regular,HanaMinB"`，对照 [sources/SquirrelTheme.swift:307-334](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L307-L334) 手动模拟 `decodeFonts`：
   - 第一段 `PingFangSC-Regular` 匹配 `family-style` 正则 → 家族 `PingFangSC`、字形 `Regular`；
   - 第二段 `HanaMinB` 不含连字符 → 当字体名 `HanaMinB`；
   - 返回数组 `[<PingFangSC-Regular>, <HanaMinB>]`。
2. 对照 [sources/SquirrelTheme.swift:293-305](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L293-L305) 模拟 `combineFonts`：以第 0 个为主，第 1 个的描述符进 `cascadeList`。

**需要观察的现象：** 解析阶段用的是系统**默认字号**（`Self.defaultFontSize`），真实字号在 `combineFonts` 的 `size` 参数处才生效；多字体通过 `cascadeList` 合并成**单个** `NSFont`，而不是绘制时手动切换字体。

**预期结果：** 你能画出这样的链路：`"PingFangSC-Regular,HanaMinB"` →（decodeFonts，按逗号拆分 + 正则分类）→ `[NSFont, NSFont]` →（combineFonts，第二项描述符进 cascadeList）→ 单个带级联表的 `NSFont`，绘制时拉丁/简体走主字体、罕用汉字自动回落到 `HanaMinB`。

#### 4.3.5 小练习与答案

**练习 1：** 如果用户写 `font_face: "Avenir,HanaMinA,HanaMinB"`，最终 `cascadeList` 里有几个字体描述符？

**参考答案：** `combineFonts` 把 `fonts[1...]`（即第 1 个之后的所有字体）放进 `cascadeList`，所以 `HanaMinA` 与 `HanaMinB` 两个描述符都在级联表里，`Avenir` 是主字体。级联查找顺序为：主字体 `Avenir` → `HanaMinA` → `HanaMinB`。

**练习 2：** 为什么 `decodeFonts` 要用 `seenFontFamilies` 去重？

**参考答案：** 同一个家族（family）挂多次没有意义——级联查找是按家族级别回退的，重复挂载只会浪费描述符、且可能在 `family-style` 与纯 `name` 两种写法混用时产生歧义。去重保证每个家族至多出现一次，级联表干净且查找确定。

### 4.4 candidate_format 中 %@/%c 归一化

#### 4.4.1 概念说明

`candidate_format` 是个**模板字符串**，描述每个候选行长什么样。Squirrel 用**具名占位符** `[label]`（编号，如「1.」）、`[candidate]`（候选词，如「你好」）、`[comment]`（注释，如拼音「ni hao」）来标记三段内容的位置。默认模板是 `'[label]. [candidate] [comment]'`，渲染出来类似 `1. 你好 ni hao`。

但历史上 Rime 用的是另一套占位符：`%@` 表示「候选词 + 注释」、`%c` 表示「编号」（见 YAML 里的 `clean_white: '%c %@'`、`apathy: "%c %@ "`）。为了向后兼容旧配置，Squirrel 在 `candidateFormat` 的 **setter** 里做了一层**归一化**：把旧占位符翻译成新占位符。这样下游（`SquirrelPanel`）只需处理统一的具名占位符，不必同时认两套语法。

#### 4.4.2 核心流程

归一化发生在 `candidateFormat` 的 `set`：

1. 拿到用户新设的模板 `newValue`。
2. 若包含 `%@`，把它替换成 `[candidate] [comment]`（一个 `%@` 展开成「候选词 + 注释」两段）。
3. 若包含 `%c`，把它替换成 `[label]`。
4. 把结果存进底层存储 `_candidateFormat`。

注意顺序：`%@` 先于 `%c` 处理。由于替换串里不含 `%`，两步互不干扰。归一化是**幂等**的——已经具名的模板（如默认的 `'[label]. [candidate] [comment]'`）不含 `%@`/`%c`，替换不发生，原样保存。

下游消费侧（`SquirrelPanel.update`）拿到的永远是具名模板：它先把整个模板串做成富文本，对 `[candidate]`/`[comment]` 区间加颜色属性，最后把 `[label]`/`[candidate]`/`[comment]` 三个字面量替换成真实文字。

#### 4.4.3 源码精读

归一化 setter：

[sources/SquirrelTheme.swift:175-188](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L175-L188) 第 175 行是计算属性 `candidateFormat`，第 178-187 行是 `set`。第 180-182 行把 `%@` 替换成 `[candidate] [comment]`，第 183-185 行把 `%c` 替换成 `[label]`，第 186 行写入 `_candidateFormat`。`get` 直接返回 `_candidateFormat`。底层存储 `_candidateFormat` 的初值见 [sources/SquirrelTheme.swift:80](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L80)。

`load` 中两次写入 `candidateFormat`（第一次读全局，第二次读配色方案覆盖）：

[sources/SquirrelTheme.swift:208](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L208) 读全局 `style/candidate_format`；[sources/SquirrelTheme.swift:260](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L260) 在配色方案分支里读 `prefix/candidate_format` 覆盖。两处都经 setter 归一化。

下游如何消费归一化后的模板：

[sources/SquirrelPanel.swift:227-253](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L227-L253) 第 227 行用 `candidateFormat` 整串建富文本；第 228、235 行对 `[candidate]`/`[comment]` 区间加属性；第 250、252、253 行把 `[label]`/`[candidate]`/`[comment]` 字面量替换成真实文字。注意第 212 行还用 `candidateFormat.contains(/\[label\]/)` 判断模板里有没有编号占位符，以决定是否要生成编号文字。

YAML 里两套语法的并存样例：

[data/squirrel.yaml:62-65](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L62-L65) 注释明确说明：`%@`/`%c` 自 1.0 起废弃，`%@` 自动展开为 `[candidate] [comment]`、`%c` 替换为 `[label]`——这正是 setter 归一化逻辑的文档化表述。

#### 4.4.4 代码实践

**实践目标：** 验证 `%@` 与 `%c` 的归一化结果，并理解旧模板为何能与新渲染器兼容。

**操作步骤：**

1. 对照 [sources/SquirrelTheme.swift:175-188](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L175-L188)，手动归一化 YAML 里的几个真实模板：
   - `clean_white` 的 `'%c %@'` → ?
   - `apathy` 的 `"%c %@ "` → ?（` ` 是四分之一个 em 空格，不受替换影响）
   - 默认 `'[label]. [candidate] [comment]'` → ?
2. 确认归一化后每个结果都只含 `[label]`/`[candidate]`/`[comment]` 三种具名占位符。

**需要观察的现象：** `%@` 永远展开成 `[candidate] [comment]` 两段（中间一个半角空格），`%c` 永远变成 `[label]`。模板里的其他字面字符（空格、` `、`.`、顿号等）原样保留。

**预期结果：**
- `'%c %@'` → `'[label] [candidate] [comment]'`（`%c`→`[label]`，`%@`→`[candidate] [comment]`，中间原有空格保留，故 label 与 candidate 间一个空格）。
- `"%c %@ "` → `"[label] [candidate] [comment] "`。
- 默认模板不含 `%@`/`%c`，原样保留 `'[label]. [candidate] [comment]'`。

所以「`%@` 被归一化成 `[candidate] [comment]`、`%c` 被归一化成 `[label]`」就是本模块的结论。归一化让 `SquirrelPanel` 只需识别一套具名占位符即可同时支持新旧配置。

#### 4.4.5 小练习与答案

**练习 1：** 为什么归一化放在 setter 而不是 getter？

**参考答案：** 放 setter 是「写时转换一次」，之后读取（`get`）和下游消费拿到的都是已归一化的纯净具名模板，无需每次读取都重复替换，性能更好且行为可预测。放 getter 则每次访问都要替换，且 `SquirrelPanel` 第 212 行的 `contains(/\[label\]/)` 判断会因未归一化而误判（旧模板含 `%c` 不含 `[label]`）。

**练习 2：** 如果用户写 `candidate_format: '%c. %@'`，渲染单个候选时编号和候选词之间显示什么？

**参考答案：** 归一化为 `'[label]. [candidate] [comment]'`。编号 `[label]` 与候选 `[candidate]` 之间是原文里的 `. `（点 + 空格），所以显示形如 `1. 你好 ni hao`（`.` 与空格都来自模板字面量）。

## 5. 综合实践

**综合任务：** 把本讲四个模块串起来，为 Squirrel 设计（或阅读）一个完整的自定义配色方案，并解释它在 `load` 中每一步的命运。

**操作步骤：**

1. 在 `~/Library/Rime/squirrel.yaml`（参考 [data/squirrel.yaml](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml) 的结构）里新增一个配色方案 `preset_color_schemes/my_theme:`，要求：
   - 设置 `color_space: display_p3`；
   - 定义 `back_color`、`text_color`、`candidate_text_color`、`hilited_candidate_text_color`、`hilited_candidate_back_color`、`comment_text_color`（按 4.2 讲的字节序 `0xAABBGGRR` 写颜色）；
   - **覆盖三个全局项**：`candidate_list_layout: linear`、`corner_radius: 10`、`font_face: 'PingFangSC-Regular,HanaMinB'`、`candidate_format: '%c %@'`。
2. 把 `style/color_scheme: my_theme` 写好，执行 `Squirrel --reload` 部署。
3. 对照 [sources/SquirrelTheme.swift:197-289](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L197-L289) 解释发生了什么：
   - 4.1：先读到全局基线（`stacked`/`Avenir`/默认模板等）；
   - 4.2：进入配色方案分支，`native=false`，按 `display_p3` 读颜色，并用 `?=` 用你的覆盖项替换掉全局的 `stacked`→`linear`、圆角、字体、模板；
   - 4.3：`PingFangSC-Regular,HanaMinB` 经 `decodeFonts` 拆成两字体、`combineFonts` 组成带 cascadeList 的 `NSFont`；
   - 4.4：`%c %@` 在 setter 里归一化成 `[label] [candidate] [comment]`，供面板渲染。

**需要观察的现象：** 候选面板变为横向线性排列、圆角变大、字体换成苹方（罕用字回落花園明朝）、编号与候选词之间是空格分隔。

**预期结果：** 你能完整复述「全局基线 → 配色方案颜色 + 覆盖项 → 字体级联 → 模板归一化」四步在 `load` 里的发生顺序，并用 `?=` 解释为什么没在方案里覆盖的项（如 `line_spacing`）会保留全局值。

（待本地验证：若暂无法构建 App，可纯做「源码阅读型实践」——只完成步骤 1 的 YAML 编写与步骤 3 的口头推演，对照源码逐行确认每条配置被哪一行代码消费。）

## 6. 本讲小结

- `SquirrelTheme.load(config:dark:)` 采用「全局 `style/*` 基线 → 配色方案叠加覆盖」两层结构，第一层读布局/几何/字体等默认值，第二层按 `color_scheme`（或暗色 `color_scheme_dark`）覆盖。
- `?=` 运算符是覆盖语义的核心：配色方案里某项非 nil 才覆盖，否则保留全局基线；这正是「换配色可连带改排版」的实现原理。
- 配色方案可覆盖几乎所有全局 style 项（布局、行为、几何、字体、`candidate_format` 等），不止颜色；`native` 方案表示跟随系统语义色，`available=false` 表示根本没配色方案。
- 字体串经 `decodeFonts`（逗号拆分 + family-style 正则 + 去重）解析成数组，再经 `combineFonts` 用 `cascadeList` 级联成单个 `NSFont`，解决拉丁字体缺汉字字形的问题。
- `candidate_format` 的旧占位符 `%@` 归一化为 `[candidate] [comment]`、`%c` 归一化为 `[label]`，归一化在 setter 写时一次性完成，让下游面板只认一套具名占位符。
- 颜色按 Rime 历史字节序 `0xAABBGGRR` 解析，并由 `color_space`（`display_p3`/`sRGB`）决定 `NSColor` 的构造方式；缺失的高亮色会回退到同组主色。

## 7. 下一步学习建议

- 接下来学习 **u3-l4 亮/暗主题与 schema 特化样式**：本讲的 `load(config:dark:)` 是被 `loadSettings` 以亮/暗两次调用、并在 schema 切换时叠加 schema 特化 `style` 节的，u3-l4 会把这条上游调用链与 `native`/`available` 标志的运行时含义讲透。
- 学完 u3-l4 后进入第四单元（候选词面板 UI）：本讲产出的 `SquirrelTheme`（颜色、字体、`candidateFormat`、`attrs` 属性字典）正是 [sources/SquirrelPanel.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift) 的 `update` 与 [sources/SquirrelView.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift) 的 `draw` 的输入，届时你会看到主题属性如何被实际绘制。
- 想深入字体系统可阅读 Apple 官方关于 `NSFontDescriptor` 的 `cascadeList` 与字体回退（font fallback）机制的文档，对照本讲 `combineFonts` 理解。
