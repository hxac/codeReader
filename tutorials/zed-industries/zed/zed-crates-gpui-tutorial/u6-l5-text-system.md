# 文本系统：字体、字形与行布局

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `TextSystem` 与 `WindowTextSystem` 的分工：前者负责字体解析、度量和光栅化，后者在前者之上叠加「按帧缓存的行布局」。
2. 解释 `FontId` / `Font` / `FontMetrics` / `FontRun` / `TextRun` 这一串类型各自在文本管线中的位置。
3. 描述一条字符串如何经过 `shape_line` / `shape_text` 变成 `ShapedLine` / `WrappedLine`，最终在 paint 阶段逐字形提交绘制。
4. 理解字形如何经由 `RenderGlyphParams` 命中光栅化缓存与精灵图集（sprite atlas），只光栅化一次、反复复用。
5. 掌握 `ShapedLine` 与 `LineLayout` 的职责分层：`ShapedLine` 因内联 `DecorationRun` 的 `SmallVec` 体积接近 3KB，新版把「切分」与「绘制」下沉为 `LineLayout::split_at` / `paint` / `paint_background`，调用方可以只持有轻量的 `Arc<LineLayout>` 并自行管理装饰。

## 2. 前置知识

本讲建立在 u3-l5（内置元素：text、img、svg、canvas）之上，你需要记得：

- **元素三阶段**：`request_layout`（申报尺寸）→ `prepaint`（记录 bounds）→ `paint`（提交绘制）。文本元素在这三个阶段里分别完成「测量、定位、画字形」。
- **StyledText 与 TextRun 的约定**：样式区间延迟到布局期才与继承的文本样式合成 `TextRun`，区间端点必须落在 UTF-8 字符边界上。

在此之上，本讲再补几个排版领域的基础术语：

- **字形（glyph）**：字体文件里的一个可绘制单元。字符 `a` 是文本概念，字形是字体概念——同一个字符在不同字体、不同上下文（连字、组合附加符）里可能对应不同字形。
- **整形（shaping）**：把「字符串 + 字体 + 字号」翻译成「一串带像素坐标的字形」的过程。它要处理连字（`fi` 可能合成一个字形）、双向文本、组合附加符（泰文元音符号与基字符同位绘制）等，所以必须交给专业的整形引擎（各平台后端内部使用 HarfBuzz 等），GPUI 只定义接口。
- **advance（步进宽度）**：从当前字形的绘制原点到下一个字形原点的水平距离。一行的宽度就是整形得到的各字形位置推进的结果，不等于「字符数 × 单字符宽度」。
- **em 与 units_per_em**：字体的原始度量以「字体单位」表示，`units_per_em` 是一个 em 方块包含的字体单位数。换算成像素的公式是：

  \[ \text{像素值} = \frac{\text{字体单位值}}{\text{units\_per\_em}} \times \text{font\_size} \]

- **ascent / descent**：基线（baseline）以上/以下的推荐距离。行高与基线偏移的关系是：

  \[ \text{baseline} = \frac{\text{line\_height} - \text{ascent} - \text{descent}}{2} + \text{ascent} \]

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/text_system.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs) | 文本子系统总入口：`TextSystem`（字体解析、度量、光栅化入口）、`WindowTextSystem`（整形入口 + 帧缓存）、`Font`/`FontWeight`/`FontStyle`/`TextRun`/`FontMetrics`/`RenderGlyphParams` 等类型的定义 |
| [src/text_system/line_layout.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs) | `LineLayout` / `ShapedRun` / `ShapedGlyph`（排好版的几何结果）、`LineLayout::split_at`（本讲重点）、`WrappedLineLayout`、以及帧缓存 `LineLayoutCache` |
| [src/text_system/line.rs](https://github.com/zed-industries-zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs) | `ShapedLine` / `WrappedLine` / `DecorationRun`，以及真正把字形画出去的 `paint_line` / `paint_line_background` 内部函数 |
| src/platform.rs | `PlatformTextSystem` trait：各平台字体后端的契约（整形、度量、光栅化都委托给它） |
| src/window.rs | `Window` 持有每窗口的 `WindowTextSystem`；`paint_glyph` / `paint_emoji` 把单个字形送进图集与场景 |
| [src/elements/text.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs) | `StyledText` 等元素的实现，是把「窗口文本系统」接到「元素三阶段」上的示范调用方 |
| [examples/text_layout.rs](https://github.com/zed-industries-zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/text_layout.rs) | 文本对齐、下划线、删除线、高亮的可运行示例 |

阅读建议：本讲主线是「一条文本的一生」，顺序为 4.1（字体从哪来）→ 4.3（文本怎么排）→ 4.2（排好的结果怎么缓存）→ 4.4（字形怎么上屏）→ 4.5（新分层 API）。

## 4. 核心概念与源码讲解

### 4.1 TextSystem 与平台字体后端：FontId、Font 与 FontMetrics

#### 4.1.1 概念说明

`TextSystem` 是 GPUI 文本渲染的「字体仓库 + 度量台」。它回答三类问题：

1. **这个名字的字体在哪？** —— 把 `Font`（字体族名 + 字重 + 样式 + features + fallbacks 的描述）解析成轻量的 `FontId`。
2. **这个字体长什么样？** —— 提供 `FontMetrics`（ascent、descent、units_per_em 等）与单个字符的包围盒、步进宽度。
3. **某个字形怎么光栅化？** —— 对外只暴露 `raster_bounds` / `rasterize_glyph` 两个入口，实际工作全部委托给平台后端。

平台差异被 `PlatformTextSystem` trait 隔离：macOS 用 CoreText，Linux/Windows 用 custro/rustskia 类后端，测试环境用 noop 实现。GPUI 核心只依赖这份契约。

#### 4.1.2 核心流程

一次字体解析的流程：

```text
调用方给出 Font { family, weight, style, ... }
        │
        ▼
TextSystem::font_id(font)
        ├─ 命中 font_ids_by_font 缓存 ──► 直接返回 FontId
        └─ 未命中 ──► platform_text_system.font_id(font)
                      └─ 结果（含错误）写回缓存
        │
        ▼ （失败时）
TextSystem::resolve_font 依次尝试 fallback_font_stack
        └─ 全部失败才 panic
```

度量查询同理：`read_metrics` 先查 `font_metrics` 表，未命中才向平台后端要一次并缓存——度量是纯函数，值得永久缓存；而行布局随字号、样式变化，只做帧级缓存（见 4.2）。

#### 4.1.3 源码精读

`FontId` 是对平台字体表下标的新type包装，`Copy + Hash`，可以在缓存键里廉价使用：

- [src/text_system.rs:L35-L42](https://github.com/zed-industries-zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L35-L42) —— `FontId(usize)` 与 `FontFamilyId(usize)`，字体与字体族的句柄。

`TextSystem` 的字段几乎全是缓存表，唯一的「真实工作者」是 `platform_text_system`：

- [src/text_system.rs:L50-L59](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L50-L59) —— `font_ids_by_font`（Font→FontId）、`font_metrics`（FontId→FontMetrics）、`raster_bounds`（光栅化包围盒缓存）、`wrapper_pool` / `font_runs_pool`（两个对象池，避免每行分配）。注意 `TextSystem` 自身**没有**行布局缓存——那是 `WindowTextSystem` 的事。

构造时硬编码了一条跨平台回退栈，从 Zed 自带字体一路退到 Arial：

- [src/text_system.rs:L63-L85](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L63-L85) —— `fallback_font_stack`：`.ZedMono` → `.ZedSans` → `Helvetica` → `Segoe UI` → 各 Linux 桌面常见字体。这就是为什么应用请求一个不存在的字体时界面仍能显示文字。

`resolve_font` 是「尽力解析 + 兜底」策略的实现：

- [src/text_system.rs:L148-L166](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L148-L166) —— 先试目标字体，再逐个试回退栈，全失败才 panic 并列出所有尝试过的族名。

度量缓存用「可升级读锁」实现先读后写、只在未命中时升级为写锁：

- [src/text_system.rs:L292-L304](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L292-L304) —— `read_metrics`：命中即读，未命中升级锁并向平台后端取一次 `FontMetrics`。

`FontMetrics` 里的原始值以字体单位存储，配套方法负责按字号换算成像素：

- [src/text_system.rs:L1101-L1175](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L1101-L1175) —— `ascent(font_size)` 等方法都遵循第 2 节的换算公式；`baseline_offset`（[L279-L290](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L279-L290)）则实现了行高与基线的居中关系。

平台契约集中在 `PlatformTextSystem`：

- [src/platform.rs:L1072-L1103](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1072-L1103) —— 从 `add_fonts` / `font_id` 到 `layout_line`（整形一条行）/ `rasterize_glyph`（光栅化一个字形）的完整清单。GPUI 核心从不直接碰字体文件，全部经由此 trait。

#### 4.1.4 代码实践（阅读型）

1. **实践目标**：验证字体回退栈真实存在，并理解度量缓存。
2. **操作步骤**：
   - 运行 `cargo run -p gpui --example text_layout`（文本示例，不依赖自定义字体）。
   - 在 `resolve_font`（[src/text_system.rs:L148](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L148)）入口加一行 `println!("resolving font family: {}", font.family);`，再运行。
   - 把示例里的某个 `div().child("Text left")` 换成带 `font_family` 的样式并不必要——改为观察日志里出现的族名即可。
3. **需要观察的现象**：终端会打印 `.SystemUIFont` 等族名；请求的字体解析失败时会看到回退栈中的族名被依次尝试。
4. **预期结果**：同一字体只会向平台后端请求一次，之后命中 `font_ids_by_font` 缓存不再打印（可在 `font_id` 的未命中分支加日志验证）。
5. 运行期行为**待本地验证**（本环境无显示服务，未实际运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FontId` 用 `usize` 而不是直接把 `Font` 结构体当作键到处传？

**答案**：`Font` 含 `SharedString` 族名和若干样式字段，拷贝、比较、哈希都贵；`FontId(usize)` 是 `Copy` 的四字节数字，可直接放进整形输入（`FontRun`）与缓存键。`font_ids_by_font` 表维护两者的映射，且缓存了解析结果（包括解析失败）。

**练习 2**：`TextSystem::resolve_font` 在所有回退都失败时选择 panic 而不是返回 `Result`，这与 CLAUDE.md「避免 panic」的规范冲突吗？

**答案**：不冲突。规范要求的是不用 `unwrap()` 这类**可预期失败**却 panic 的写法；而「整个系统连一个可用字体都没有」属于不可恢复的安装级故障——继续运行必然到处崩，不如启动即失败并打印完整的回退栈清单，这是刻意的 fail-fast。

**练习 3**：`em_advance` 与 `em_layout_width`（[src/text_system.rs:L226-L235](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L226-L235) 与 [L713-L716](https://github.com/zed-industries-zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L713-L716)）都返回「一个 em 的宽度」，它们有什么区别？

**答案**：`em_advance` 用单字形的 advance（步进）计算，是把字形度量除以 `units_per_em` 再乘字号，**不经过整形**；`em_layout_width` 则真跑一次 `layout_line` 整形，返回整形后的行宽。整形可能应用字距微调（kerning）与连字，所以两者在个别字体上会有亚像素差异。需要与屏幕上实际排版一致的宽度时用后者。

### 4.2 WindowTextSystem 与帧内布局缓存 LineLayoutCache

#### 4.2.1 概念说明

`TextSystem` 是应用级单例（`App` 持有），但**整形结果是窗口帧的产物**：同一行文本在这一帧排过，下一帧大概率原样再画。于是 GPUI 在 `TextSystem` 之上叠了一层 `WindowTextSystem`：

- 它 `Deref` 到 `TextSystem`，所以字体/度量方法照常可用；
- 额外持有 `LineLayoutCache`，一个「当前帧 + 上一帧」双缓冲的行布局缓存；
- 每个窗口各有一份（在 `Window::new` 时创建），帧结束时 `finish_frame` 把当前帧翻转为上一帧。

这与 u4-l3 讲过的「元素树每帧重建、缓存显式 opt-in」是一致的哲学：布局结果不属于保留状态，而是**帧间可复用的临时产物**——只用上一帧的缓存垫一下「重复排版同一行」这个最高频的开销。

#### 4.2.2 核心流程

一帧内请求排版一条行的路径：

```text
shape_line(text, font_size, runs)          （样式级 API，返回 ShapedLine）
        │  合并相邻同装饰的 TextRun → DecorationRun
        ▼
layout_line(text, font_size, runs)         （把 TextRun 解析成 FontRun）
        │
        ▼
LineLayoutCache::layout_line               （以 CacheKey 查缓存）
        ├─ 当前帧命中 ──► 直接返回 Arc<LineLayout>
        ├─ 上一帧命中 ──► 搬到当前帧并登记 used_lines
        └─ 未命中 ──► platform.layout_line 整形
                      连同 CacheKey 存入当前帧
帧结束：Window::draw 收尾调用 finish_frame
        交换 current/previous，清空新的 current
```

缓存键 `CacheKey` = 文本内容 + 字号 + `FontRun` 列表 + 换行宽度 + 强制宽度（[src/text_system/line_layout.rs:L886-L893](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L886-L893)）。任何影响字形位置的因素都进键，键相同即可安全复用。

#### 4.2.3 源码精读

`WindowTextSystem` 的定义极其克制——就是「TextSystem + 行布局缓存」：

- [src/text_system.rs:L362-L377](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L362-L377) —— `#[derive(Deref)]` 加 `#[deref]` 标注：对外是一个类型，内部两个字段。
- [src/window.rs:L1418-L1421](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L1418-L1421) —— 每个窗口创建自己的 `WindowTextSystem`；元素代码里 `window.text_system()`（[src/window.rs:L2111-L2114](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L2111-L2114)）拿到的就是它。

缓存本体是 `previous_frame`（Mutex）+ `current_frame`（RwLock）两张表：

- [src/text_system/line_layout.rs:L454-L477](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L454-L477) —— `FrameCache` 里 `lines` / `wrapped_lines` 两类条目各配一张 map 加一个 `used_*` 登记表；`used_*` 决定「哪些条目这帧被访问过」，配合 `reuse_layouts` / `truncate_layouts`（[L496-L557](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L496-L557)）供 `.cached()` 视图按录制区间批量复活布局——这与 u4-l3 的缓存重放是同一机制在文本层的落点。

命中优先级「当前帧 → 上一帧 → 真整形」：

- [src/text_system/line_layout.rs:L639-L690](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L639-L690) —— `LineLayoutCache::layout_line`：先用可升级读锁查当前帧；未命中升级写锁、从上一帧**搬走**条目（`remove_entry`）；仍未命中才调 `platform_text_system.layout_line` 真整形，并把结果 `Arc` 化后写回。换行版 `layout_wrapped_line`（[L574-L637](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L574-L637)）在此基础上先排版未换行布局，再调 `compute_wrap_boundaries` 算断行点。

帧翻转发生在一帧绘制的收尾：

- [src/window.rs:L2920-L2922](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L2920-L2922) —— `Window::draw` 末尾：清空 Taffy 布局树、`text_system().finish_frame()`、场景 `finish`。三者顺序对应「布局树、文本布局、绘制场景」三类每帧重建的资源。
- [src/text_system/line_layout.rs:L559-L572](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L559-L572) —— `finish_frame`：交换两张表后清空新的 current。上一帧没用到的条目就此被丢弃——「被连续两帧访问」是布局缓存的生存条件。

另有按内容哈希寻址的旁路缓存 `lines_by_hash`（[L467-L476](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L467-L476) 与 [L700-L746](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L700-L746)）：调用方（如编辑器）持有分片数据时可以不拼接完整字符串、只用哈希探测缓存，miss 时才物化文本。这是为 Zed 编辑器海量行渲染准备的优化，初学阶段知道其存在即可。

#### 4.2.4 代码实践（阅读型）

1. **实践目标**：观察「上一帧命中」路径的触发条件。
2. **操作步骤**：在 `LineLayoutCache::layout_line` 的三个返回点分别加 `println!("line cache: hit-current / hit-prev / miss")`；运行 `cargo run -p gpui --example text_layout`，然后拖动窗口宽度。
3. **需要观察的现象**：首帧大量 miss；静止时后续帧大多 hit-current 或 hit-prev；改变窗口宽度导致 `wrap_width` 变化时，换行版条目重新 miss（缓存键变了）。
4. **预期结果**：静止界面的稳态下几乎没有真整形调用；任何影响 `CacheKey` 的改动（字号、字体、宽度）都会让对应行重新整形。
5. 具体打印比例**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FontRun` 池（`font_runs_pool`）要放在 `WindowTextSystem` 而度量缓存放在 `TextSystem`？

**答案**：两者生命周期不同。`FontRun` 是**一次整形调用的临时输入**，每个 `layout_line`/`shape_text` 调用都要一个 Vec，用对象池避免高频分配；度量是**字体属性的纯函数**，与窗口、帧无关，进程内缓存一份即可，归 `TextSystem`。

**练习 2**：缓存为什么「从上一帧 remove_entry 搬走」而不是 clone？

**答案**：搬走保证同一 `Arc<LineLayout>` 不会同时出现在两张表里，`finish_frame` 交换后，没被访问过的上一帧条目连同 map 一起被清空释放；如果 clone，上一帧表会一直持有所有条目，等于缓存永不淘汰。

**练习 3**：`layout_line` 检查缓存的顺序是「当前帧 → 上一帧 → 整形」，可不可以省掉当前帧那次查找？

**答案**：不可以省。同一帧内同一行文本可能被测量多次（例如 Taffy 的 intrinsic size 探测先量一次，正式布局再量一次）；当前帧命中是纯读、无锁升级成本，还避免重复登记 `used_lines`。省掉它会让同帧重复访问都走「从上一帧搬家」的写路径，得不偿失。

### 4.3 Line/TextRun：一条文本如何变成排好版的行

#### 4.3.1 概念说明

这一模块把两套「run（区间）」类型分清楚：

- **`TextRun`**：**样式**区间。调用方的输入——「接下来 N 个 UTF-8 字节用这个字体、这个颜色、这些下划线/背景」。它是 `WindowTextSystem` 公开 API 的参数类型。
- **`FontRun`**：**整形**区间。「接下来 N 个字节用 `FontId` 对应的字体整形」。颜色、下划线等视觉信息不参与整形，所以在进入平台后端前会被剥离掉。
- **`DecorationRun`**：**绘制**区间。整形后画字形时需要的最小样式集（颜色、背景、下划线、删除线），相邻同装饰的 `TextRun` 会被合并。

排好版的结果是 `LineLayout`：一行文本的完整几何——总宽、ascen/descent、以及一串 `ShapedRun`（同一字体的字形段），每个 `ShapedGlyph` 记录「字形 id + 在行内的像素位置 + 它对应原文的字节下标 + 是否 emoji」。**字节下标 `index` 是字形世界与文本世界的桥**：光标定位、选中、按字节切分（4.5）全靠它。

#### 4.3.2 核心流程

```text
调用方
  │  text + &[TextRun]（样式区间，长度之和须覆盖整段文本）
  ▼
WindowTextSystem::shape_line（单行，不允许含 \n）
  │  ① 合并相邻同装饰 TextRun → DecorationRun（SmallVec）
  │  ② layout_line：逐 TextRun resolve_font → FontRun（同字体且装饰未变则合并）
  │  ③ LineLayoutCache 查缓存/整形 → Arc<LineLayout>
  ▼
ShapedLine { layout, text, decoration_runs }        ←— 可直接 paint
```

多行文本走 `shape_text`：按 `\n` 切行，把跨行的 `TextRun` 按行边界裁开重新分配，每行得到一个 `WrappedLine`（内含 `WrappedLineLayout`：未换行布局 + 换行边界 + 可选 wrap_width）。

坐标↔下标的双向映射是编辑器交互的基石：

```text
x_for_index(byte_index)  ：字节下标 → 像素 x（找第一个 index >= 目标的字形，取其 position.x）
index_for_x(x)           ：像素 x → 字节下标（从后往前找第一个 position.x <= x 的字形）
closest_index_for_x(x)   ：同上但取「最近」边界，用于上下方向键对齐
```

#### 4.3.3 源码精读

`TextRun` 的字段全景：

- [src/text_system.rs:L985-L1000](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L985-L1000) —— `len` 是 **UTF-8 字节数**（不是字符数）；`font` 完整描述字体；其余是颜色、背景、下划线、删除线。区间必须首尾相接覆盖全文，否则整形端会告警「TextRuns do not cover the entire text」。
- [src/text_system/line_layout.rs:L874-L880](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L874-L880) —— `FontRun { len, font_id }`：整形的全部输入。

`shape_line` 的三步：

- [src/text_system.rs:L391-L436](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L391-L436) —— 注意开头的 `debug_assert`（单行 API 收到 `\n` 直接断言失败）与装饰合并循环：相邻 run 的颜色/下划线/删除线/背景全相同就并入前一个，只累加 `len`。这把装饰数量压到最小，paint 阶段的切换次数随之减少。

`TextRun → FontRun` 的翻译在 `WindowTextSystem::layout_line`：

- [src/text_system.rs:L641-L694](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L641-L694) —— 逐 run `resolve_font`；前一个 `FontRun` 字体相同**且装饰未变化**才合并。为什么装饰变化也要打断 FontRun？因为 `LineLayout` 里的 run 边界同时被 `DecorationRun` 对齐消费（见 4.5 的 paint），保持两者边界一致能让绘制循环少做字节区间对账。

排好版的几何结果：

- [src/text_system/line_layout.rs:L14-L54](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L14-L54) —— `LineLayout`（font_size、width、ascent、descent、runs、len）与 `ShapedRun` / `ShapedGlyph`。`ShapedGlyph.index` 记录原文字节下标，`position` 是行内坐标（x 从 0 开始累进 advance）。

双向映射的实现就是「在 runs 上线性扫」：

- [src/text_system/line_layout.rs:L57-L71](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L57-L71) —— `index_for_x`：从最后一个 run 倒着找第一个 `position.x <= x` 的字形。
- [src/text_system/line_layout.rs:L104-L114](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L104-L114) —— `x_for_index`：正序找第一个 `index >= 目标` 的字形，返回其 x。**4.5 的 `split_at` 正是用它确定切点宽度**。

多行与换行：

- [src/text_system.rs:L506-L635](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L506-L635) —— `shape_text`：`process_line` 闭包负责把跨行 `TextRun` 裁剪到当前行（`run.len -= run_len_within_line`）、生成该行的装饰与 FontRun、调 `layout_wrapped_line`；`line_clamp` 会随已产出的行数递减剩余可换行数（截断省略号就建立在这上面）。
- [src/text_system/line_layout.rs:L190-L269](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L190-L269) —— `compute_wrap_boundaries`：在**已整形**的字形流上按 `wrap_width` 找断行点（优先在空白/词边界处断），产出 `WrapBoundary { run_ix, glyph_ix }`。注意它是后置断行——先整形整行，再决定在哪里折，因此 `WrappedLineLayout` 保存的是「未换行布局 + 断行点集合」，绘制时按断行点换行重排（`paint_line` 里 `glyph_origin.y += line_height` 的那段）。

元素层如何串起来（示范调用方）：

- [src/elements/text.rs:L696-L780](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L696-L780) —— `TextLayout::layout` 在 measure 回调里调 `window.text_system().shape_text(...)`，累加每行 `size(line_height)` 得到元素尺寸，并把 `lines` 存进元素状态。
- [src/elements/text.rs:L792-L827](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L792-L827) —— `TextLayout::paint` 在 paint 阶段逐行先 `paint_background`（背景色块）再 `paint`（字形 + 下划线），行原点 y 逐行累加。

#### 4.3.4 代码实践（阅读型）

1. **实践目标**：用一个已存在的示例印证「TextRun 划分样式」。
2. **操作步骤**：阅读并运行 [examples/text_layout.rs:L77-L82](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/text_layout.rs#L77-L82)——`StyledText::new("ABCD").with_highlights([(0..1, FontWeight::EXTRA_BOLD.into()), (2..3, FontStyle::Italic.into())])`，即 `cargo run -p gpui --example text_layout`。
3. **需要观察的现象**：`A` 加粗、`C` 斜体、其余常规；下划线与删除线在三种对齐下的绘制位置。
4. **预期结果**：`StyledText` 的 highlights 最终会被合成为两个 `TextRun`（加粗段与斜体段）加上前后常规段，交给 `shape_line`/`shape_text`——你看到的字重/字形差异就是不同 `Font` 解析成不同 `FontId` 后分段整形的直接结果。
5. 运行期外观**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`TextRun.len` 填「字符数」会发生什么？

**答案**：`len` 是 UTF-8 字节数。填字符数时区间总长小于文本总长，`shape_text` 的 `process_line` 会打出 `` `TextRun`s do not cover the entire to be shaped text `` 告警（[src/text_system.rs:L530-L533](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L530-L533)），尾部文本落入最后一个装饰区间；含多字节字符时切点还可能不在字符边界上，绘制对账会错位。中文等 CJK 文本尤其容易踩中。

**练习 2**：`shape_line` 与 `shape_text` 的适用场景分别是什么？`shape_line` 为什么禁止 `\n`？

**答案**：`shape_line` 面向「逻辑上就是一行」的调用方（编辑器的一屏内一行、表格单元格），结果是 `ShapedLine`，无换行概念；`shape_text` 面向自然语言段落，接受 `\n` 与 `wrap_width`，结果是多行 `WrappedLine`。`\n` 的整形没有意义（字形流中不存在换行字形），混入会破坏「len 覆盖、下标对齐」的契约，所以用 `debug_assert` 拦截。

**练习 3**：`x_for_index` 在目标下标没有任何字形对应（例如落在被连字吞掉的字符上）时返回什么？

**答案**：返回**第一个满足 `glyph.index >= index` 的字形的 x**；若整行都不满足（下标超过所有字形），返回 `self.width`（行尾）。也就是说它给出的是「该下标处笔尖应当所在的 x」，天然适合光标定位——连字 `fi` 中间按下标取 x 会得到连字字形自己的 x，光标贴在连字起点。

### 4.4 字形光栅化缓存与图集：从 paint_glyph 到屏幕

#### 4.4.1 概念说明

整形回答「画哪些字形、画在哪」，光栅化回答「这个字形的像素长什么样」。GPUI 的做法是经典的**精灵图集（sprite atlas）**：

- 每个字形按「字体 + 字形 id + 字号 + 缩放系数 + 亚像素变体 + 渲染模式 + 膨胀级别」唯一确定一份位图，用 `RenderGlyphParams` 作键；
- 位图只光栅化一次，之后作为 tile 存进平台图集（GPU 纹理），绘制时变成一次带 alpha 的贴图（`MonochromeSprite` / `SubpixelSprite` 图元）；
- 亚像素定位把连续的像素坐标量化到有限档位，让「同一字形在不同小数位置」收敛为有限个缓存条目。

这里的取舍是：**文本绘制从「逐字符画矢量」变成「查表 + 贴图」**，代价是图集占显存、量化引入最多半个像素档位的定位误差。

#### 4.4.2 核心流程

`paint_line`（4.5 详述）遍历字形，对每个可见字形调 `window.paint_glyph(origin, font_id, glyph_id, font_size, color)`：

```text
paint_glyph
  ① origin 乘 scale_factor 换算设备像素
  ② 亚像素量化：x' = round(x·N)/N，N = SUBPIXEL_VARIANTS_X(4)
     subpixel_variant = (x'·N 的小数部分) 作为档位编号
  ③ 组装 RenderGlyphParams { font_id, glyph_id, font_size,
       subpixel_variant, scale_factor, is_emoji,
       subpixel_rendering, dilation }
  ④ text_system.raster_bounds(params)   ← 永久缓存（RwLock 表）
  ⑤ sprite_atlas.get_or_insert_with(params, rasterize_glyph)
       ├─ 图集已有 tile ──► 直接取
       └─ 没有 ──► 真正光栅化一次并上传图集
  ⑥ 向本帧场景插入 MonochromeSprite / SubpixelSprite 图元
```

量化公式：

\[ x_{\text{quant}} = \frac{\mathrm{round}(x \cdot N)}{N}, \quad N = 4 \]

即 x 方向只有 4 个亚像素档位（y 方向 `SUBPIXEL_VARIANTS_Y = 1`，即按整像素对齐）。

#### 4.4.3 源码精读

- [src/text_system.rs:L44-L48](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L44-L48) —— 亚像素档位常量：X 方向 4 档、Y 方向 1 档。
- [src/text_system.rs:L1016-L1032](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L1016-L1032) —— `RenderGlyphParams`：缓存键的八个维度。注意 `dilation`（按颜色决定的字形增粗级别）也在键里——浅色小字需要轻微增粗才不显瘦，同一字形不同颜色可能对应不同位图。
- [src/text_system.rs:L323-L343](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L323-L343) —— `TextSystem::raster_bounds`（带永久缓存的包围盒查询）与 `rasterize_glyph`（真正调平台后端光栅化，返回尺寸 + 像素字节）。

核心在 `Window::paint_glyph`：

- [src/window.rs:L4261-L4300](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4261-L4300) —— 步骤①-④：缩放、量化（[L4275-L4285](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4275-L4285) 的 `round_half_toward_zero` 即量化公式的实现）、组装参数、查包围盒。
- [src/window.rs:L4300-L4335](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4300-L4335) —— 步骤⑤⑥：`sprite_atlas.get_or_insert_with(...)` 惰性光栅化并取 tile；按 `subpixel_rendering` 决定插入 `SubpixelSprite`（携带 RGB 通道各自的覆盖信息）还是 `MonochromeSprite`（单通道 alpha，乘上颜色绘制）。emoji 走 `paint_emoji`（[L4366-L4397](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4366-L4397)）：彩色位图直接贴图，没有颜色参数。

亚像素渲染的启用条件相当严格：

- [src/window.rs:L4339-L4356](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4339-L4356) —— 窗口背景必须不透明（亚像素依赖已知底色合成）、平台支持、且渲染模式允许。这也解释了为什么图集键里要有 `subpixel_rendering`：同一个字形可能同时存在亚像素与单色两份位图。

图集契约：

- [src/platform.rs:L1324-L1336](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L1324-L1336) —— `PlatformAtlas::get_or_insert_with`：以 `AtlasKey` 查 tile，未命中调用 build 回调生成位图。GPU 纹理的装箱（用 etagere 箱装箱算法，见 Cargo.toml 依赖）在平台实现里完成。

#### 4.4.4 代码实践（观察型）

1. **实践目标**：直观感受「字形位图只生成一次」。
2. **操作步骤**：在 `TextSystem::rasterize_glyph`（[src/text_system.rs:L336](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L336)）入口加 `println!("rasterize glyph {:?}", params.glyph_id);`；运行 `cargo run -p gpui --example text_layout`，等界面稳定后观察输出停止增长；再拖一个新窗口尺寸（缩放系数变化会生成新参数）。
3. **需要观察的现象**：启动阶段每个「字形×字号×档位」组合打印一次；静止后不再打印；缩放系数或字号改变时又出现一批新条目。
4. **预期结果**：光栅化次数 ≈ 屏幕上出现的不同字形组合数，与文本重绘次数无关——重绘只是贴图。
5. **待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 X 方向量化成 4 档而不是对齐到整像素？

**答案**：整像素对齐会让每个字形在水平方向「抖动」最多 1 像素，小字号文本的可读性明显受损；4 个亚像素档位在缓存条目数（×4）与定位精度（¼ 像素）之间取平衡。Y 方向只有 1 档（`SUBPIXEL_VARIANTS_Y = 1`），因为垂直方向按行高排布、字形内部相对基线的偏移已由整形给出，亚像素收益小。

**练习 2**：`dilation` 为什么进 `RenderGlyphParams` 键？

**答案**：`glyph_dilation_for_color`（[src/text_system.rs:L345-L348](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L345-L348)）按颜色返回增粗级别（浅色小字需要膨胀）。增粗改变位图本身，所以同一 (字体, 字形, 字号, 档位) 下不同颜色可能是两张不同的位图，必须入键；否则浅色文本会错误复用未膨胀的位图而显得过细。

**练习 3**：`paint_glyph` 最后插入场景的是 `order: 0` 的图元，那文本的层叠顺序由什么决定？

**答案**：由 `Scene` 的 `BoundsTree` 按「最大相交 order 加一」赋予图元 z-order（u4-l3 讲过的机制）；`paint_glyph` 写死的 `order: 0` 只是占位，`Scene::finish` 排序批次时会统一重排。这也是为什么 `paint_line` 要先用 `window.paint_layer(line_bounds, ...)` 建层（见 4.5）——一行的字形与下划线作为同一批图元参与排序。

### 4.5 ShapedLine 与 LineLayout 的分层：split_at / paint / paint_background

#### 4.5.1 概念说明

这一模块讲本讲最重要的新设计。先看两个类型各自的构成：

- **`LineLayout`**：纯几何（`font_size` / `width` / `ascent` / `descent` / `runs` / `len`），外加少量查询方法。没有颜色、没有原文、没有装饰——**它描述「字形在哪」，不描述「长什么样」**。
- **`ShapedLine`**：`Arc<LineLayout>` + 原文 `SharedString` + `decoration_runs: SmallVec<[DecorationRun; 32]>`。它把「几何 + 文本 + 装饰」打包，`paint` 时用自己的装饰直接可画，是最方便的调用方接口。

问题出在体积上：`SmallVec<[DecorationRun; 32]>` 按容量**内联**预留 32 个 `DecorationRun`，每个含颜色、可选背景、可选下划线与删除线样式，仅内联缓冲就接近 3KB——`ShapedLine` 按值传递、按值返回时这 3KB 都要跟着搬家。对编辑器里常见的「把一行切成多个碎片再拼排」场景（软换行、括号对齐粘滞列、行内块折叠、diff 拼接），一次排版可能产生成百上千个碎片，反复搬运与重分配的代价就变得刺眼。

于是有了新分层：

- **`LineLayout::split_at(byte_index)`**：在几何层把布局切成前后两半，返回两个**轻量**的 `LineLayout`；
- **`LineLayout::paint` / `paint_background`**：接收**显式传入**的 `decoration_runs` 切片——调用方自己管理装饰；
- **`ShapedLine::split_at`**：保留为便捷入口，内部委托 `LineLayout::split_at`，并额外替你切分装饰区间与文本。

这样，碎片化拼行的调用方可以长期持有裸 `Arc<LineLayout>`（克隆只是一次引用计数），只在绘制瞬间以 `&[DecorationRun]` 借出装饰，避免近 3KB 的 `ShapedLine` 在碎片之间反复复制。

#### 4.5.2 核心流程

`LineLayout::split_at(byte_index)` 的契约（[doc 注释](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L128-L134)原文归纳）：

```text
x_offset = x_for_index(byte_index)          ← 切点的前进宽度

对每个 ShapedRun：
  split_pos = 字形数组中第一个 index >= byte_index 的位置（partition_point）
  [0, split_pos)      → 左半 run（保持原位置）
  [split_pos, ..)     → 右半 run：
        position.x -= x_offset               ← 重定位到 x=0
        index     -= byte_index              ← 字节下标重基到 0

左半 LineLayout { width: x_offset,          len: byte_index }
右半 LineLayout { width: 原宽 - x_offset,    len: 原len - byte_index }
font_size / ascent / descent 原样复制给两半
```

三条不变量（源码内建测试逐一验证）：

1. \( \text{left.width} + \text{right.width} = \text{line.width} \)
2. \( \text{left.len} + \text{right.len} = \text{line.len} \)
3. 右半首字形 `position.x == 0`，且所有字节下标从 0 重新起算。

**下标重基是双刃剑**：右半可以直接当作「一行新文本」用（`x_for_index`、再 `split_at` 都成立），但你也必须**自己重基装饰区间**——传旧装饰会导致颜色错位。`ShapedLine::split_at` 帮你做了这步；`LineLayout::split_at` 的调用方要自己做（综合实践会专门验证这一点）。

#### 4.5.3 源码精读

先看便捷层 `ShapedLine` 的构成与体积来源：

- [src/text_system/line.rs:L22-L50](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L22-L50) —— `DecorationRun`（len + 颜色/背景/下划线/删除线）与 `ShapedLine { layout: Arc<LineLayout>, text, decoration_runs: SmallVec<[DecorationRun; 32]> }`。内联 32 容量的 SmallVec 即「近 3KB」的来源；`#[deref]` 让 `shaped_line.width()` 这类几何方法直接透传到 `LineLayout`。

`ShapedLine` 的两个 paint 方法是「自带装饰」入口：

- [src/text_system/line.rs:L82-L130](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L82-L130) —— `ShapedLine::paint` / `paint_background` 只是把自己的 `decoration_runs` 与空 `wrap_boundaries` 传给内部函数 `paint_line` / `paint_line_background`。

新的底层入口直接挂在 `LineLayout` 上：

- [src/text_system/line.rs:L207-L263](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L207-L263) —— `LineLayout::paint` / `paint_background`：签名里多了 `decoration_runs: &[DecorationRun]` 参数，doc 明说这是「持有裸布局、自管装饰的调用方」的 lower-level 替代。

`LineLayout::split_at` 本体：

- [src/text_system/line_layout.rs:L128-L188](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L128-L188) —— 全部几何切分逻辑。注意 [L143](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L143) 的 `partition_point`：一个 run 可以同时给两半供字形（切点落在 run 中间）；[L153-L161](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L153-L161) 完成右半的「减 x_offset、减 byte_index」重基。

`ShapedLine::split_at` 委托几何层、补齐装饰与文本：

- [src/text_system/line.rs:L132-L204](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L132-L204) —— 第一行就是 `self.layout.split_at(byte_index)`；随后遍历装饰区间：完全在切点左侧/右侧的整体归属，**跨越切点的区间被拆成两个**并各自调整 `len`（[L157-L174](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L157-L174)）；文本同样按字节切成两段 `SharedString`。边界情形（`byte_index == 0` / `== len`）直接复用原文本避免分配。

真正执行绘制的 `paint_line`（内部函数，两个公开 paint 都汇到这里）：

- [src/text_system/line.rs:L344-L405](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L344-L405) —— 先 `window.paint_layer(line_bounds, ...)` 建层（一行的字形/下划线作为同批图元）；按 `aligned_origin_x` 计算对齐后的起点；随后双层循环遍历 `runs → glyphs`，用 `glyph.position.x - prev_glyph_position.x` 的**增量**推进笔尖（字形位置天然是累进坐标，增量推进使换行重排、切分重定位都能无缝工作）；每当 `glyph.index >= run_end` 就从装饰迭代器取下一段样式；命中 content_mask 的字形才调 `paint_glyph` / `paint_emoji`（[L529-L553](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L529-L553)）——这就是 4.4 的入口。
- [src/text_system/line.rs:L739-L760](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L739-L760) —— `aligned_origin_x`：Left/Center/Right 三种对齐的起点公式。右对齐即 `origin.x + align_width - line_width`——综合实践用它验证「测宽 + 右对齐」。

内建测试是理解契约最好的材料：

- [src/text_system/line.rs:L802-L851](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L802-L851) —— `test_split_at_invariants`：在 "abcdef" 的每个字节位置切分，断言宽度、长度、文本拼接、字体度量四条不变量。
- [src/text_system/line.rs:L853-L948](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L853-L948) —— `test_split_at_glyph_rebasing`：两个字体 run（模拟 fallback 边界）连续切分，验证右半 run 数量、下标重基到 0、宽度守恒。
- [src/text_system/line.rs:L950-L1024](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L950-L1024) —— `test_split_at_decorations`：红/绿/蓝三段装饰在切点 3 处，跨界的绿段被拆成 1+2。

顺带一提 `force_width`（等宽对齐）也在 `LineLayout` 层处理：

- [src/text_system/line_layout.rs:L844-L872](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L844-L872) —— `apply_force_width_to_layout`：把字形 x 吸附到 `glyph_pos * force_width` 的网格上，同时识别零步进的组合附加符（泰文元音等）不推进格子——终端类 UI 的等宽要求与 HarfBuzz 的自然排版之间的调和层。

#### 4.5.4 代码实践（阅读型 + 单元验证）

1. **实践目标**：不写 GUI 也能验证 `split_at` 的不变量。
2. **操作步骤**：运行 `cargo test -p gpui split_at`。
3. **需要观察的现象**：三个测试（invariants / glyph_rebasing / decorations）通过。
4. **预期结果**：`left.width + right.width == line.width`、右半下标重基、跨界装饰拆分全部成立。你也可以仿照 `make_shaped_line` 辅助函数（[src/text_system/line.rs:L769-L800](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L769-L800)）构造一个含双字节字符（如 `你好`）的行，在 3 字节处切分，观察左半 `len == 3` 而右半首字形下标为 0。
5. 该测试不依赖显示服务，**可以直接运行验证**；含 CJK 的新测试属示例代码，需自行添加到 `mod tests` 中验证。

#### 4.5.5 小练习与答案

**练习 1**：既然有了 `ShapedLine::split_at`，为什么还要 `LineLayout::split_at`？删掉后者、让所有调用方都用前者不行吗？

**答案**：`ShapedLine::split_at` 返回的是两个完整 `ShapedLine`——每个都携带近 3KB 的内联装饰 SmallVec、一份新的 `SharedString` 文本、两个新的 `Arc`。碎片化拼行（软换行、粘滞列、行内折叠）一次要切几十刀、产物存活一整帧，反复按值搬运这些大结构正是要避免的开销。`LineLayout::split_at` 只切几何（两个轻量结构体），调用方持有 `Arc<LineLayout>` 复用、装饰自己管，`ShapedLine` 反过来变成「偶尔需要一站式切分时」的便捷包装。

**练习 2**：切分点落在多字节字符的中间（`byte_index` 不在字符边界上）会怎样？

**答案**：`x_for_index` 返回第一个 `index >= byte_index` 的字形的 x——也就是**下一个完整字符**的起点，几何上安全；但左半 `len = byte_index` 会让左半的「文本长度」与字形覆盖不一致，`ShapedLine::split_at` 里 `&self.text[..byte_index]` 还会直接 panic（Rust 字符串切片要求字符边界）。所以调用方必须保证切点在 UTF-8 边界上，这与 `TextRun` 端点的要求一致。

**练习 3**：`LineLayout::paint` 为什么不把 `TextAlign` / `align_width` 也交给装饰切片，而是留在参数里？

**答案**：对齐是**整行的几何属性**（取决于这一行要贴着多宽的容器排），装饰是**逐字节区间的视觉属性**。拼行场景下两者经常来自不同来源：碎片布局决定自己多宽、外层容器决定对齐宽度。把它们分开，同一个碎片布局可以在不同对齐策略下复用，不必重新整形。

## 5. 综合实践

把本讲四个模块串成一个可运行示例：渲染一段「带语法高亮的代码」，先整行右对齐，再在注释起点用 `LineLayout::split_at` 切成两半分别绘制，最后用打印验证三条不变量。

**实践目标**：

1. 用 `TextRun` 划分关键字（加粗紫色）、字符串（绿色）、注释（灰色）三种样式；
2. 用 `shape_line` 测量整行宽度，配合 `TextAlign::Right` 实现右对齐；
3. 用 `layout_line` 拿到裸 `Arc<LineLayout>`，`split_at` 切分后分别 `paint`，验证「前半宽度 == 切点 x 进度」与「后半首字形重定位到 x=0」；
4. 体会「裸 LineLayout 自管装饰」：右半的字节下标已重基，装饰必须自己重基，否则注释颜色会错位。

**操作步骤**：

1. 新建 `examples/text_split.rs`（Cargo 会自动发现 examples 目录下的新文件，无需改 Cargo.toml），写入以下**示例代码**（本讲新增，非仓库原有）：

   ```rust
   #![cfg_attr(target_family = "wasm", no_main)]

   use gpui::{
       App, Bounds, Context, DecorationRun, Font, Hsla, SharedString, TextAlign, TextRun,
       Window, WindowBounds, WindowOptions, canvas, div, font, point, prelude::*, px, rgb,
       size,
   };
   use gpui_platform::application;

   fn deco(len: u32, color: Hsla) -> DecorationRun {
       DecorationRun {
           len,
           color,
           background_color: None,
           underline: None,
           strikethrough: None,
       }
   }

   struct TextSplitView;

   impl Render for TextSplitView {
       fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
           div().size_full().p_4().child(canvas(
               |bounds, _, _| bounds, // prepaint：把 bounds 原样传给 paint
               |bounds, _bounds, window: &mut Window, cx: &mut App| {
                   let text: SharedString = "let msg = \"hi\"; // greet".into();
                   let font_size = px(16.);
                   let line_height = px(24.);
                   let base: Font = font(".SystemUIFont");

                   // ① 用 find 计算三段样式的字节边界（避免手算字节）
                   let keyword_end = "let".len();
                   let string_start = text.find('"').unwrap();
                   let string_end = text[string_start + 1..]
                       .find('"')
                       .map(|ix| string_start + 1 + ix + 1)
                       .unwrap();
                   let comment_start = text.find("//").unwrap();

                   let keyword_color: Hsla = rgb(0xc678dd).into();
                   let string_color: Hsla = rgb(0x98c379).into();
                   let comment_color: Hsla = rgb(0x8a8a8a).into();
                   let plain: Hsla = rgb(0x333333).into();

                   let run = |len: usize, font: Font, color: Hsla| TextRun {
                       len,
                       font,
                       color,
                       background_color: None,
                       underline: None,
                       strikethrough: None,
                   };
                   let runs = vec![
                       run(keyword_end, base.clone().bold(), keyword_color),
                       run(string_start - keyword_end, base.clone(), plain),
                       run(string_end - string_start, base.clone(), string_color),
                       run(comment_start - string_end, base.clone(), plain),
                       run(text.len() - comment_start, base.clone(), comment_color),
                   ];

                   let text_system = window.text_system();

                   // ② 整行 shape + 测宽 + 右对齐绘制（第一行）
                   let shaped = text_system.shape_line(text.clone(), font_size, &runs, None);
                   println!("整行宽度 = {:?}", shaped.width());
                   shaped
                       .paint(
                           bounds.origin,
                           line_height,
                           TextAlign::Right,
                           Some(bounds.size.width),
                           window,
                           cx,
                       )
                       .unwrap();

                   // ③ 裸 LineLayout：在注释起点切分（第四行展示）
                   let layout = text_system.layout_line(&text, font_size, &runs, None);
                   let x_at_split = layout.x_for_index(comment_start);
                   let (left, right) = layout.split_at(comment_start);
                   println!(
                       "切点 x 进度 = {x_at_split:?}，前半宽度 = {:?}",
                       left.width
                   );
                   println!(
                       "后半首字形 x = {:?}，后半长度 = {}",
                       right.runs.first().and_then(|r| r.glyphs.first()).map(|g| g.position.x),
                       right.len
                   );

                   let row = bounds.origin + point(px(0.), line_height * 3.);
                   // 左半：装饰沿用原行前缀的区间（关键信息：装饰长度按左半的字节下标）
                   let left_decorations = vec![
                       deco(keyword_end as u32, keyword_color),
                       deco((string_start - keyword_end) as u32, plain),
                       deco((string_end - string_start) as u32, string_color),
                       deco((comment_start - string_end) as u32, plain),
                   ];
                   left.paint(
                       row,
                       line_height,
                       TextAlign::Left,
                       None,
                       &left_decorations,
                       window,
                       cx,
                   )
                   .unwrap();

                   // 右半：字节下标已重基到 0，装饰必须自己重基——
                   // 整段就是注释色，所以一条覆盖 right.len 的区间即可
                   let right_decorations = vec![deco(right.len as u32, comment_color)];
                   let right_origin = row + point(left.width, px(0.));
                   right.paint(
                       right_origin,
                       line_height,
                       TextAlign::Left,
                       None,
                       &right_decorations,
                       window,
                       cx,
                   )
                   .unwrap();
               },
           ))
       }
   }

   fn run_example() {
       application().run(|cx: &mut App| {
           let bounds = Bounds::centered(None, size(px(800.0), px(300.0)), cx);
           cx.open_window(
               WindowOptions {
                   window_bounds: Some(WindowBounds::Windowed(bounds)),
                   ..Default::default()
               },
               |_, cx| cx.new(|_| TextSplitView),
           )
           .unwrap();
           cx.activate(true);
       });
   }

   #[cfg(not(target_family = "wasm"))]
   fn main() {
       run_example();
   }
   ```

2. 运行：

   ```bash
   cargo run -p gpui --example text_split
   ```

3. 观察终端输出与窗口（第一行是整行右对齐的高亮代码；下方一行是「左半 + 右半」无缝拼回的同一行）。

**需要观察的现象与预期结果**：

- 终端三行日志中，`切点 x 进度` 与 `前半宽度` **相等**（`split_at` 用 `x_for_index` 作为左半宽度）；`后半首字形 x` 为 `0 px`（重定位）；`后半长度` 等于整行长度减注释起点。
- 窗口里两行内容字形相同：第一行右贴容器边缘（`TextAlign::Right` + `align_width`），第二行从左边界的两个碎片拼成，接缝处视觉无错位——证明「宽度守恒 + 右半重基」共同成立。
- 故意把 `right_decorations` 换成 `left_decorations` 试试：右半开头会被错误地按「关键字色」绘制前三四个字形——这就是「裸布局自管装饰、下标重基后装饰要跟着重基」的直接证据。
- 本环境无显示服务，以上运行期行为**待本地验证**；不变量部分也可用 4.5.4 的单元测试离线验证。

**排查提示**：若示例启动 panic 在 `resolve_font`，说明平台字体后端一个回退字体都找不到（常见于无 fontconfig 的极简容器）；换个有桌面环境的主机运行。

## 6. 本讲小结

- `TextSystem` 是应用级字体仓库：`Font` 描述 → `FontId` 句柄（带解析缓存与跨平台回退栈），`FontMetrics` 提供按字号换算的度量，光栅化与整形统一委托 `PlatformTextSystem`。
- `WindowTextSystem` = `TextSystem` + 每窗口的 `LineLayoutCache`：双缓冲「当前帧/上一帧」缓存，`finish_frame` 在 `Window::draw` 收尾翻转；只有连续两帧被访问的布局才能存活。
- 调用方用 `TextRun`（样式区间）描述文本，GPUI 把它翻译成 `FontRun`（整形区间）交给平台后端，得到 `LineLayout`（`ShapedRun`/`ShapedGlyph` 几何流）；`x_for_index` / `index_for_x` 借字形自带的字节下标完成坐标↔文本的双向映射。
- 字形绘制走图集：`paint_glyph` 把坐标量化到亚像素档位，组装 `RenderGlyphParams` 查 `raster_bounds` 与 `PlatformAtlas`，每个「字形×参数组合」只光栅化一次，之后全是贴图。
- 新分层把「切分」与「绘制」下沉到 `LineLayout`：`LineLayout::split_at` 只切几何（宽度守恒、右半重基到 x=0 且下标归零），`LineLayout::paint` / `paint_background` 显式接收装饰切片；携带近 3KB 内联装饰的 `ShapedLine` 退居便捷层——碎片化拼行的调用方从此可以只持有轻量 `Arc<LineLayout>` 并自管装饰。

## 7. 下一步学习建议

- **u6-l6（文本换行与测量：line_wrapper）**：本讲的 `compute_wrap_boundaries` 是「整形后断行」，下一讲讲「整形前按宽度断行」的 `LineWrapper`——1500 行的独立算法模块，两者互为补充。
- 阅读 [src/elements/text.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs) 中 `TextLayout` 的 `index_for_position` / `position_for_index` 等公开方法，看编辑器如何把本讲的行布局映射到光标坐标。
- 想看 `LineLayout::split_at` 的真实消费者，可以在仓库范围内搜索 `layout.split_at` / `LineLayout::paint` 的调用点（Zed 编辑器 crate 中的软换行与行内块拼行是典型场景）。
- 回顾 u4-l3 的「帧缓存重放」与本讲 `reuse_layouts` / `truncate_layouts` 的关系，理解 `.cached()` 视图为什么能连文本布局一起复活。
