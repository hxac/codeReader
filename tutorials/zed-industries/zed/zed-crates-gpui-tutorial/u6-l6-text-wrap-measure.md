# 文本换行与测量:line_wrapper

## 1. 本讲目标

上一讲(u6-l5)我们看懂了「一段文本如何被整形(shaping)成带字形位置的 `LineLayout`」。这一讲回答一个更具体的问题:**当一行文本的宽度超过容器的宽度时,应该在哪里断开?断开之后,这「一个逻辑行」到底占多高?**

学完本讲,你应该能够:

1. 说出 GPUI 中**两条换行路径**的分工:整形前的逐字符估算路径(`LineWrapper`)与整形后的字形位置路径(`compute_wrap_boundaries`)。
2. 读懂 `LineWrapper::wrap_line` 的贪心断行算法:词边界回退、CJK 任意断、悬挂缩进(hanging indent)。
3. 理解 `LineWrapperHandle` 的池化(pooling)设计,以及它为什么能省下大量重复的单字符宽度测量。
4. 掌握 `.truncate()`、`.text_ellipsis()`、`.line_clamp(n)` 这些样式方法如何一路走到换行与截断代码,并用「边界数 + 1」× 行高计算出换行后的内容高度。

## 2. 前置知识

本讲默认你已从前面几讲了解了以下概念,这里做一次快速回顾与补充:

- **整形(shaping)与字形(glyph)**:文本渲染前要把「字符序列 + 字体」交给平台文本系统,得到一串字形及其前进宽度(advance)。u6-l5 讲过:字形的位置在 `LineLayout` 的 `runs` 里,每个字形还带 `index`(原文本的字节下标)。
- **kerning(字距调整)**:某些字符对(如 `AV`)排在一起时会比单独测量再相加更窄。这意味着**逐字符宽度累加是对真实整形宽度的「高估」**——本讲会看到源码里专门为这一点打的补丁。
- **`AvailableSpace`**:u4-l2 讲过 Taffy 的三态约束(Definite/MinContent/MaxContent)。文本元素在 `request_measured_layout` 的 measure 回调里正是从它拿到「可用宽度」,进而决定换行宽度。
- **`WhiteSpace` 语义**:借自 CSS。`Normal` 表示允许在空白处换行;`Nowrap` 表示禁止换行、文本溢出容器。
- **视觉行 vs 逻辑行**:一段不含 `\n` 的长文本,在窄容器里会被折成多行显示。本讲把「换行前的一整段」叫**逻辑行**,换行后的每一截叫**视觉行**。关键心智模型:**GPUI 不为每个视觉行单独建 `LineLayout`,而是在一份「未换行布局」上记录一串断点,绘制时按断点折行**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/text_system/line_wrapper.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs) | 本讲主角。`LineWrapper` 结构、`wrap_line` 断行算法、截断家族(`truncate_line` / `truncate_wrapped_line`)、词字符白名单 `is_word_char`、逐字符宽度缓存,以及约 900 行测试 |
| [src/text_system.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs) | `TextSystem` 上与换行相关的三个入口:`line_wrapper()`(池化句柄)、`layout_width()`(单字符宽度)、`shape_text`/`shape_line`(承接 u6-l5) |
| [src/text_system/line_layout.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs) | 第二条换行路径:`LineLayout::compute_wrap_boundaries`、`WrappedLineLayout`、`WrapBoundary`、`size()` 行高公式 |
| [src/text_system/line.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs) | `WrappedLine`:携带断点信息的可绘制行,`paint` 时按断点折行 |
| [src/elements/text.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs) | 样式与算法之间的粘合层:`TextLayout::layout` 的 measure 回调,决定 `wrap_width`、调用截断、汇总尺寸 |
| [src/styled.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/styled.rs) | 样式侧入口:`whitespace_nowrap`、`text_ellipsis` 家族、`truncate`、`line_clamp` |
| [examples/text_wrapper.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/text_wrapper.rs) | 换行与截断的演示示例,是本讲实践的基础 |

## 4. 核心概念与源码讲解

### 4.1 模块一:换行发生在哪——两条路径的分工

#### 4.1.1 概念说明

一个自然的疑问:换行为什么不交给布局引擎 Taffy?因为 Taffy 只知道「这个文本元素要占多大的盒子」,而**断在哪一个字符之间,只有文本系统自己知道**。所以 GPUI 的分工是:Taffy 通过 measure 回调(见 u4-l2 的 `request_measured_layout`)向文本系统询问尺寸,文本系统在回答之前先自己算好换行。

更有意思的是,GPUI 里有**两套**计算换行的代码:

1. **整形后路径**:先把整行文本完整整形(得到每个字形真实的 x 坐标),再扫描字形位置找断点。产出 `WrapBoundary { run_ix, glyph_ix }`(定位到「第几个 run 的第几个字形」)。这条路径服务于**正常渲染**。
2. **整形前估算路径**:不整形整行,只用「单个字符的宽度」逐字符累加来判断哪里超宽。产出 `Boundary { ix, next_indent }`(定位到原文本的字节下标)。这条路径就是本讲的 `LineWrapper::wrap_line`,主要服务于**截断**(`text_ellipsis`、`line_clamp`)——截断需要在整形**之前**先决定「保留哪一段文本」,此时还没有字形可用。

为什么估算路径可行?因为断行只需要一个近似宽度:超宽了就回退到上一个词边界。宁可略有偏差,也不能为每个候选断点做一次完整整形。

#### 4.1.2 核心流程

两条路径最终汇合于同一个消费端:`WrappedLine`。整体流程:

```text
样式(white_space / text_overflow / line_clamp)
        │
        ▼
TextLayout::layout 的 measure 回调(取 wrap_width,必要时先截断)
        │
        ├── [截断] LineWrapper::truncate_line / truncate_wrapped_line   ← 估算路径
        │              (依赖 width_for_char 的逐字符宽度缓存)
        ▼
window.text_system().shape_text(text, size, runs, wrap_width, line_clamp)
        │
        ▼
layout_wrapped_line → 整形出 LineLayout → compute_wrap_boundaries    ← 整形后路径
        │
        ▼
WrappedLineLayout { unwrapped_layout, wrap_boundaries, wrap_width }
        │
        ▼
WrappedLine::paint:遍历字形,命中 WrapBoundary 就折行
```

#### 4.1.3 源码精读

整形后路径的核心是 [`LineLayout::compute_wrap_boundaries`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L190-L269)。它把所有 run 的字形摊平成一个迭代器,每个元素是 `(WrapBoundary, 字符, 字形的 x 坐标)`,然后扫描:

- [line_layout.rs:222-243](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L222-L243):跳过 `\n`;用 `LineWrapper::is_word_char` 判定词字符,在「空格后的第一个词字符」处记录可回退的候选断点 `last_candidate_ix`——注意这里**直接复用了** `LineWrapper` 的白名单,两套算法共享词边界判定。
- [line_layout.rs:245-248](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L245-L248):宽度用**相邻字形的 x 坐标差** `next_x - last_boundary_x` 计算——这是真实整形宽度,天然包含 kerning。
- [line_layout.rs:249-254](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L249-L254):`line_clamp` 生效时,断点数达到 `max_lines - 1` 就提前 `break`,后续内容不再计算。

源码里有一句非常诚实的注释,点破了两套代码的关系——[line_layout.rs:227-228](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L227-L228):「与 `LineWrapper::wrap_line` 非常相似,但存在差异,所以在这里复制了一份代码」。差异主要有三处:本路径操作字形坐标而非字符宽度、支持 `max_lines` 提前终止、不计算悬挂缩进。

产出的 [`WrapBoundary`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L285-L291) 与估算路径的 `Boundary` 是一对平行类型,可以对照记忆:

| | 估算路径 `Boundary` | 整形后路径 `WrapBoundary` |
| --- | --- | --- |
| 定位方式 | 原文本字节下标 `ix` | `run_ix` + `glyph_ix`(双下标) |
| 额外信息 | `next_indent`(悬挂缩进) | 无 |
| 宽度来源 | 逐字符宽度累加(高估,无 kerning) | 字形 x 坐标差(精确) |
| 使用时机 | 整形前(截断) | 整形后(渲染) |

#### 4.1.4 代码实践

**实践目标**:亲手追踪一次「换行宽度如何走进 `compute_wrap_boundaries`」,建立调用链的肌肉记忆。

**操作步骤**(源码阅读型实践):

1. 打开 [src/elements/text.rs:738-746](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L738-L746),确认 `TextLayout::layout` 的 measure 回调最后调用 `shape_text(text, font_size, &runs, wrap_width, text_style.line_clamp)`,把换行宽度和行数上限一并传下去。
2. 用 Grep 在 `src/text_system.rs` 中找到 `pub fn shape_text`(约 509 行处),顺着它的实现走到 `layout_wrapped_line`。
3. 打开 [src/text_system/line_layout.rs:574-637](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L574-L637),注意 611-615 行:`wrap_width` 为 `Some` 时才调用 `compute_wrap_boundaries`,否则断点列表为空(即不换行)。
4. 运行演示示例(命令在沙箱中**待本地验证**):
   ```bash
   cargo run -p gpui --example text_wrapper
   ```

**需要观察的现象**:示例窗口最底部那块没有加任何截断样式的文本(约 109 行的 `div().text_xl().w_full().child(text)`)会随窗口宽度自动换行;拖窄窗口,行数变多。

**预期结果**:你能不借助 IDE 的跳转功能,口头复述「样式 → measure 回调 → shape_text → compute_wrap_boundaries → WrapBoundary 列表」这条链路上每一步的输入输出。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `compute_wrap_boundaries` 的宽度用相邻字形 x 坐标之差,而不是把每个字形的 advance 相加?

**答案**:字形位置是整形器(compiler)排出来的,已经包含 kerning、连字(ligature)等上下文效果;把 advance 相当作各自独立的宽度会丢掉这些修正,窄于真实宽度,可能把实际放得下的行误判为超宽。

**练习 2**:`line_clamp(3)` 最终在 `compute_wrap_boundaries` 里如何体现?

**答案**:`max_lines = Some(3)` 被传入,当已收集的断点数达到 `3 - 1 = 2` 个时提前 `break`,第 3 个视觉行之后的内容连断点都不再计算,配合外层 `overflow_hidden` 实现裁剪。

### 4.2 模块二:`wrap_line` 贪心断行算法与词字符白名单

#### 4.2.1 概念说明

[`LineWrapper::wrap_line`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L40-L44) 解决的问题是:给定一组**行片段**(fragment)、一个换行宽度,产出一串断点。输入不只是一个 `&str`,而是 `&[LineFragment]`——因为一行里可能混着「固定宽度的非文本元素」(比如行内图标):

- [line_wrapper.rs:607-621](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L607-L621):`LineFragment` 有两个变体——`Text`(一段字符串)和 `Element`(给定像素宽度 + 它在逻辑文本里占的 UTF-8 字节数,便于断点下标仍然对得上原文本)。

算法是经典的**贪心 + 回退**:从左到右累加字符宽度;一旦超过 `wrap_width`,优先回退到最近一次记录的「安全断点」(词边界),如果压根没有安全断点(比如一个超长的词),就在当前字符处硬断。

「安全断点」的定义由 [`is_word_char`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L450-L484) 白名单决定——它回答「哪些字符可以粘在一个词里不被拆开」。

#### 4.2.2 核心流程

`wrap_line` 返回一个惰性迭代器(每次 `next` 产出一个 `Boundary`),单步逻辑的伪代码:

```text
状态: width(当前视觉行已占宽度), last_candidate_ix/width(最近安全断点),
      last_wrap_ix(上次断点), indent(悬挂缩进), prev_c(前一字符)

对每个候选(字符 c 或元素):
  若 c == '\n':跳过(换行符不占宽度)
  记录安全断点:
    若 c 是词字符 且 prev_c == ' ' 且本行已有非空白 → 安全断点 = 此处
    若 c 不是词字符 且 c != ' ' 且本行已有非空白   → 安全断点 = 此处(CJK 任意断)
    (元素片段:prev_c == ' ' 时也可作安全断点)
  width += 该候选宽度
  若 width > wrap_width 且当前位置在上次断点之后:
    首次超宽时,用「本行第一个非空白字符的位置」计算悬挂缩进 indent(≤ MAX_INDENT)
    若存在安全断点:回退到它(width 减去多算的部分)
    否则:在当前候选处硬断(width 重置为当前候选的宽度)
    indent 生效时,新行起点补上 indent 个空格的宽度
    产出 Boundary { ix: 断点字节下标, next_indent: indent }
```

悬挂缩进(hanging indent)的直觉:如果逻辑行以一串前导空格开头(比如代码缩进),换行后 GPUI 会让**后续视觉行也缩进同样的量**,而不是从第 0 列重新开始——这对保持视觉对齐很重要。缩进量上限 [`MAX_INDENT = 256`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L27) 防止极端深缩进把后续行挤得没有内容空间。

CJK 的特殊处理在 [line_wrapper.rs:74-79](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L74-L79):中文、日文等文字**词与词之间没有空格**,而它们又不在 `is_word_char` 白名单里,于是每个非空白的 CJK 字符都会成为安全断点——`Hello world你好世界` 可以在 `好` 与 `世` 之间断开,却不会把 `Hello` 拆成 `Hel` + `lo`。

#### 4.2.3 源码精读

**词字符白名单** [`is_word_char`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L450-L484) 分五组,值得逐组读一遍注释:

- ASCII 字母数字:`Hello123`。
- 拉丁补充/扩展、西里尔、越南语组合字符、孟加拉文:覆盖法/德/西/俄/越南/孟加拉等语言的字母,`Thậmchíđến…` 这样的长词不被拆断。
- 常见「粘性符号」`- _ . ' ’ $ % @ # ^ ~ , = : ;`:`var_name`、`3.1415`、`@mention`、`Self::new`、`on;` 保持完整——尾部标点跟随前词,断行不会把逗号孤零零留在下一行行首。
- Zed 专用字符 `⋯`。
- **不换行粘合字符**(non-breaking glue,Unicode TR14):U+202F、U+00A0、U+2011。这些是「看起来像空格/连字符但不许断」的字符,放进词字符表后,`a\u{202F}b` 整体不可拆。

**主循环** [line_wrapper.rs:57-136](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L57-L136) 中最值得精读的是超宽分支:

- [line_wrapper.rs:107-113](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L107-L113):触发条件是 `width > wrap_width && ix > last_wrap_ix`——第二个条件防止「单个候选自身就比 wrap_width 还宽」时在同一个位置反复产出断点。
- [line_wrapper.rs:115-122](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L115-L122):回退(`last_candidate_ix > 0`)或硬断(`width = item_width`,新行从当前这个「放不下的大块」重新开始)。
- [line_wrapper.rs:124-128](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L124-L128):悬挂缩进按「`' '` 的宽度 × indent」补进新行的 width。

产出物 [`Boundary { ix, next_indent }`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L666-L679):`ix` 是**原文本的字节下标**(所以 `LineFragment::Element` 才需要 `len_utf8`,让下标在混排时依然连续)。

**截断家族**是同一套宽度缓存的三位姊妺方法(同属 `LineWrapper`):

- [`should_truncate_line`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L141-L188):从 Start 或 End 方向扫描,提前为截断符号(affix,如 `…`)预留宽度,返回应截断的字节位置。Start 方向用 `char_indices().rev()` 从右往左扫。
- [`should_truncate_line_middle`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L190-L245):把预算切成前 \( \frac{2}{3} \) 与后 \( \frac{1}{3} \),保头保尾、挖中间,产出 `(front_end_ix, back_start_ix)` 一对下标。
- [`truncate_wrapped_line`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L310-L446):**边换行边截断**,是 `line_clamp` 的真身。注释里说得很清楚:它不像 `truncate_line` 那样把宽度预算简单乘以行数(`width * max_lines`),而是先按词边界换行找到「第 max_lines 行的起点」,再在这一行上为 affix 留出空间后截断。它的 347-418 行几乎逐行复刻了 `wrap_line` 的状态机(width、候选断点、缩进全套搬过来),419-438 行是最后一行独有的逻辑:只有 `width + affix_width <= wrap_width` 时才允许把 `truncate_ix` 推进——保证截断符号永远放得下。遇到 `\n` 时(349-375 行),若已是最后一行且后面还有真实内容,就地截断加省略号;否则换行计数加一、重置全部状态。

#### 4.2.4 代码实践

**实践目标**:用测试断言反推算法行为,直观感受「断点下标」「悬挂缩进」的含义。

**操作步骤**:

1. 阅读 [line_wrapper.rs:712-859](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L712-L859) 的 `test_wrap_line`。先看第一组断言:文本 `"aa bbb cccc ddddd eeee"` 在 `px(72.)` 下产出断点 `7, 12, 18`。自己数一数:`"aa bbb "` 恰好是 7 个字节(含尾随空格)——断点落在**空格之后**。
2. 重点看第三组(738-745 行):文本 `"     aaaaaaa"`(5 个前导空格)的三个断点 `next_indent` 全是 **5**——悬挂缩进把 5 个空格的宽度记下来,后续每个视觉行都缩进。
3. 运行这个测试(结果**待本地验证**,取决于沙箱能否编译):
   ```bash
   cargo test -p gpui --lib text_system::line_wrapper::tests::test_wrap_line
   ```

**需要观察的现象 / 预期结果**:测试通过;你能在纸上对任意一组断言复现断点下标——方法是以字节为单位标出每个字符的位置,模拟伪代码里的 width 累加与回退。

**练习(选做)**:把 761-769 行那组 `"          aaaaaaaaaaaaaa"`(10 个前导空格)的预期断点 `[7,0] [14,3] [18,3] [22,3]` 解释给自己听:为什么第一个断点 `next_indent` 是 0,第二个开始才是 3?提示:缩进在**首次超宽**时才用 `first_non_whitespace_ix` 计算,而第一断点发生在 7 个空格(约 67px)处、当时只扫过空格;第二行起点是第 7 个空格,距行首的非空白字符(第 10 列)差 3 个空格。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `"你好世界"` 四个字可以在任意两字之间断行,而 `"Hello"` 不能?

**答案**:`is_word_char` 白名单覆盖的是 ASCII 字母数字、几类拼音文字字母与粘性符号;CJK 表意文字**不在**白名单里,于是每个非空白 CJK 字符都满足「非词字符且非空格」分支,成为安全断点;`Hello` 的每个字符都是词字符,不产生断点,只能整词回退或硬断。

**练习 2**:URL `https://github.com/zed-industries/zed` 中的 `/` 和 `\` 不在白名单里,这意味着什么?

**答案**:测试 `assert_not_word("zed-industries/zed")`(1161-1162 行)正说明:`/` 两侧允许断行。这是**有意的**——超长 URL 若不许在 `/` 处断,窄容器里就只能硬断甚至溢出;相反 `.` 在白名单里,所以 `github.com` 不会在点处断开。

**练习 3**:`truncate_wrapped_line` 为什么不直接调 `truncate_line(text, wrap_width * max_lines, …)`?

**答案**:宽度预算乘行数假设每一行都被填满,忽略了两点:词边界换行会让行尾留白(实际用量小于预算),以及最后一行必须额外容纳 affix。`truncate_line` 的扁平预算会截掉本可显示的内容,或该截不截;`truncate_wrapped_line` 逐字符复刻换行状态机,先定位最后一行的起点,再在该行上为 affix 留宽,截断点精确。`max_lines <= 1` 时两者才等价,源码也正是在 319-321 行做了这个退化。

### 4.3 模块三:`LineWrapperHandle` 池化与逐字符宽度缓存

#### 4.3.1 概念说明

`wrap_line` 每走一个字符都要问一次「这个字符多宽」。这个问题的朴素解法非常贵:[`TextSystem::layout_width`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L208-L221) 的实现是把**单个字符编码成 UTF-8、构造一个 FontRun、做一次完整的 `layout_line` 整形**再取宽度。为每个字符付一次整形的代价显然不可接受——何况同一个字符(`'a'`、`','`、汉字)在一次换行里会反复出现。

`LineWrapper` 内部于是有两级缓存,见[结构定义](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L17-L23):

- `cached_ascii_char_widths: [Option<Pixels>; 128]`——ASCII 字符用**定长数组**,下标即字符码点,零哈希开销。
- `cached_other_char_widths: HashMap<char, Pixels>`——非 ASCII 字符用哈希表。

缓存是按 `(FontId, 字号)` 组合生效的:同一个 `LineWrapper` 绑定一种字体一种字号(构造参数 `font_id` + `font_size`)。于是自然产生下一个问题:**每次需要换行时都 new 一个 `LineWrapper`,缓存就永远只有一次命中就报废**。答案是池化——`LineWrapperHandle`。

#### 4.3.2 核心流程

```text
调用方: cx.text_system().line_wrapper(font, font_size)
   │  以 FontIdWithSize { font_id, font_size } 为键查 wrapper_pool
   │  池中该键的 Vec<LineWrapper> 弹出一个;池空则 LineWrapper::new 新建
   ▼
返回 LineWrapperHandle(持有 wrapper + TextSystem 的 Arc 引用)
   │  Deref/DerefMut 透传,当 &mut LineWrapper 用
   │  使用期间宽度缓存不断积累('a' 只整形一次)
   ▼
Drop: 把 wrapper 推回池中同键的 Vec —— 缓存下次继续用
```

#### 4.3.3 源码精读

- 池的存储:[text_system.rs:56](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L56),`wrapper_pool: Mutex<FxHashMap<FontIdWithSize, Vec<LineWrapper>>>`——键是[字体加字号的小结构](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L843-L847),值是**同配置 wrapper 的空闲列表**。
- 借出:[`TextSystem::line_wrapper`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L307-L321),`wrappers.pop()` 弹出复用,`unwrap_or_else(LineWrapper::new)` 池空才新建。注意它同时完成 `resolve_font`(字体名 → FontId,u6-l5 讲过的字体解析缓存)。
- 归还:[`Drop for LineWrapperHandle`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L855-L867) 把 wrapper 按 `wrapper.font_id + wrapper.font_size` 原路推回。
- 透传:[`Deref`/`DerefMut`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L869-L881) 让句柄直接当 `LineWrapper` 用,调用方感知不到池的存在。
- 两级宽度缓存:[`width_for_char`](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_wrapper.rs#L486-L507),先查 ASCII 数组再查 HashMap,都未命中才走 `layout_width` 并回填。

顺带一提,[`layout_width` 上方的注释](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system.rs#L206-L207)写着「Consider removing this?」——维护者自己也在审视这条「单字符整形」路径,它是文本系统里少见的带疑问标注的公开 API。

这套设计的收益可以粗算:一段 500 字符的英文文本,去重后不同字符通常不到 80 个;池化 + 两级缓存意味着一次换行最多触发约 80 次单字符整形,之后同一字号的文本换行几乎零整形开销。

#### 4.3.4 代码实践

**实践目标**:确认「文本元素的 measure 回调每帧都从池里借 wrapper」这一事实。

**操作步骤**(源码阅读型):

1. 打开 [elements/text.rs:696](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L696):`let mut line_wrapper = cx.text_system().line_wrapper(text_style.font(), font_size);`——这行在 measure 回调内,也就是说**每次重新测量都可能借还一次**。
2. 注意这行下面 `line_wrapper` 只在需要截断(`truncate_width` 为 `Some`)时才被真正使用;纯换行路径并不经过它(走的是 4.1 的整形后路径)。
3. 若想看得更真,可以在本地临时在 `line_wrapper()` 的 `LineWrapper::new` 分支加一条 `println!("new wrapper")`(学习后 `git checkout` 还原),然后拖动窗口宽度观察:只有**第一次**测量某字号会打印,之后全部命中池。
   涉及修改源码,仅建议在本地学习分支上做,**待本地验证**。

**预期结果**:拖动窗口时不再出现 `new wrapper` 日志,证明池在跨帧复用 wrapper 及其宽度缓存。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `cached_ascii_char_widths` 用定长数组而不用 HashMap?

**答案**:ASCII 只有 128 个码点,数组以码点为下标,查找是一次内存访问、无哈希计算与冲突处理;且 `Option<Pixels>` 初始化为 `None` 即「未缓存」,语义正好。非 ASCII 码点空间巨大(到 0x10FFFF),只能用 HashMap。

**练习 2**:如果同一个 `LineWrapper` 被同时用于 16px 和 24px 两种字号的文本,会发生什么?

**答案**:不会发生——`line_wrapper(font, font_size)` 的池按 `FontIdWithSize` 分桶,不同字号借到的是不同 wrapper;`LineWrapper` 自身把 `font_size` 存为字段并只服务这一个值。若强行混用(例如绕过池手工构造后改字号),宽度缓存会张冠李戴,断点全部错位——这正是把 `font_id`/`font_size` 设为 `pub(crate)`、只能由 `TextSystem::line_wrapper` 受控构造的原因之一。

### 4.4 模块四:从样式到行高——换行结果如何变成像素高度

#### 4.4.1 概念说明

前三个模块解决了「在哪断」。最后一个模块解决「断完之后多高」以及「样式怎么触发这一切」。

行高公式出奇地简单。一个逻辑行换成了 \( n + 1 \) 个视觉行(断点数为 \( n \)),则:

\[ \text{height} = \text{line\_height} \times (n + 1) \]

`line_height` 来自文本样式(如 `.line_height(relative(1.3))` 换算出的像素值,经 `window.pixel_snap` 吸附,见 [elements/text.rs:636-640](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L636-L640))。注意高度与**内容无关**——不 ascent/descent 逐行计算,而是统一按行高网格排布,这与等宽排版、终端式界面的习惯一致。

#### 4.4.2 核心流程

样式侧的入口都在 `Styled` trait 上(「截断三件套」+ 两个开闭换行的开关):

```text
.whitespace_nowrap()      → WhiteSpace::Nowrap      → wrap_width = None(不换行)
.text_ellipsis()          → TextOverflow::Truncate("…")       → 尾部截断
.text_ellipsis_start()    → TextOverflow::TruncateStart("…")  → 头部截断(适合文件路径)
.text_ellipsis_middle()   → TextOverflow::TruncateMiddle("…") → 中部截断
.text_overflow(自定义affix)→ TextOverflow::Truncate("…自定义")
.truncate()               → overflow_hidden + nowrap + ellipsis(Tailwind 同名)
.line_clamp(n)            → line_clamp = Some(n) + overflow_hidden
```

measure 回调里的决策顺序([elements/text.rs:647-780](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L647-L780)):

1. **定 wrap_width**:`white_space == Normal` 时取 `known_dimensions.width`,否则取 `AvailableSpace::Definite(x)`;`Nowrap` 则恒为 `None`。
2. **定 truncate_width**:有 `text_overflow` 时,可用宽度是 `Definite(x)`,且设置了 `line_clamp(n)` 时预算放大为 \( x \times n \)(单行预算×行数的近似,只用于「是否需要截断」的初判)。
3. **查缓存**:命中条件要求 `wrap_width` 一致且双方 `truncate_width` 均为 `None`——截断过的布局会把「截断后的尺寸」当作固有尺寸,污染 MinContent/MaxContent 探测,所以一律重算。
4. **截断**:有 `line_clamp` 且有 `wrap_width` → `truncate_wrapped_line`;否则先整形量一下真实宽度,确实超了才 `truncate_line`。
5. **整形 + 换行**:`shape_text(..., wrap_width, line_clamp)` 走 4.1 的整形后路径,得到 `Vec<WrappedLine>`。
6. **汇总尺寸**:高度 = Σ 每行 `size(line_height).height`;宽度 = 各行宽度的最大值。

#### 4.4.3 源码精读

- 样式方法本体:[styled.rs:82-149](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/styled.rs#L82-L149)。`truncate()` 是组合技([139-141 行](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/styled.rs#L139-L141));`line_clamp` 在设置行数的同时强制 `overflow_hidden()`([145-149 行](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/styled.rs#L145-L149)),因为换行计算已停在第 n 行,溢出必须靠裁剪兜底。
- [`TextOverflow` 枚举](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/style.rs#L405-L419):三个变体都携带 `SharedString` 作为截断符号——`text_ellipsis()` 用默认 `…`,也可以 `text_overflow(TextOverflow::Truncate(" » ".into()))` 自定义。
- 行高公式:[line_layout.rs:310-315](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L310-L315),`height: line_height * (self.wrap_boundaries.len() + 1)`——就是本节开头的公式。
- 尺寸汇总:[elements/text.rs:761-766](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L761-L766),高度逐行累加、宽度取最大值并 `ceil()`。
- **防误截补丁**:[elements/text.rs:709-723](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/text.rs#L709-L723)。这段代码是 4.2「估算高估」问题的正面回应:截断判定基于逐字符宽度累加,**没有 kerning,会高估真实宽度**,把恰好放得下的文本误截。所以先用 `shape_text` 整形出真实宽度检查一遍,真超宽才走 `truncate_line`。注释写得很清楚,值得整段读。
- 绘制侧的折行:[line.rs:355-361](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L355-L361) 先按 `line_height * (wrap_boundaries.len() + 1)` 算出 `line_bounds`(供 paint_layer 分层裁剪);随后遍历字形时,[line.rs:395-396](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L395-L396) 一旦当前字形的 `(run_ix, glyph_ix)` 等于队首断点,就消费该断点并把笔尖(glyph_origin)移到下一视觉行的行首。`WrappedLine::paint` 的签名与对齐宽度来源见 [line.rs:284-311](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line.rs#L284-L311):`bounds` 没给时用 `layout.wrap_width` 做右对齐/居中的基准——**对齐基于换行宽度而非文本自然宽度**,这也是换行与对齐能正确配合的关键。

#### 4.4.4 代码实践

**实践目标**:用一个最小改动对比四种截断/换行策略的视觉效果,并验证「估算 vs 整形」的差异真实存在。

**操作步骤**:

1. 运行基线示例(**待本地验证**):
   ```bash
   cargo run -p gpui --example text_wrapper
   ```
2. 示例已经并排展示了 `truncate()`、`line_clamp(2)`、`text_overflow(TextOverflow::Truncate("".into()))`(空截断符,即硬切)、`line_clamp(3)`、`whitespace_nowrap()` 和底部纯换行文本。对照 [examples/text_wrapper.rs:58-109](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/text_wrapper.rs#L58-L109) 的六块代码,把界面上的每一块与样式对应起来。
3. 临时实验:把 83 行的 `TextOverflow::Truncate("".into())` 改成 `TextOverflow::Truncate("……more".into())`,再改回;把 73 行 `line_clamp(2)` 依次改成 `1`、`3`、`10`。每次改完重跑。
4. 验证防误截:找一段**恰好占满一行**的短文本放进 `.truncate()` 容器(例如把 62 行的长文本临时换成短串),观察它不会被加省略号——这正是 4.4.3 说的「先整形量真实宽度」补丁的功劳;若估算路径直接判定,这段文本会被误截。

**需要观察的现象**:

- `line_clamp` 越大,可见行数越多,但超过实际行数后不再有省略号;
- 空截断符的 `text_overflow` 表现为文本在容器边缘被硬切(无 `…`);
- 自定义 affix 会占用一行宽度,可见文本变短。

**预期结果**:你能只凭样式名预判任意一块的渲染结果,并能说出每个效果分别走到了 `truncate_wrapped_line`、`truncate_line` 还是 `compute_wrap_boundaries`。

#### 4.4.5 小练习与答案

**练习 1**:`.truncate()` 与 `.text_ellipsis().overflow_hidden()`(不设 nowrap)效果有何区别?

**答案**:`truncate()` 额外包含 `whitespace_nowrap()`,即禁换行 + 尾部省略号;只设 `text_ellipsis + overflow_hidden` 时文本仍会换行,`truncate_width` 的预算按 `AvailableSpace::Definite(x)`(无 line_clamp 时不乘行数)判定,通常整段不超预算,几乎不会出现省略号。

**练习 2**:一段文本在 300px 宽容器里换成了 4 行,行高 20px,它向 Taffy 申报的测量高度是多少?若同一段文本放进 600px 容器(断点数减半)呢?

**答案**:300px 时高度 = \( 20 \times 4 = 80 \)px。600px 时断点数约为原来一半(设为 2),高度 = \( 20 \times 3 = 60 \)px。核心在于**高度完全由「断点数 + 1」与行高的乘积决定**,与 ascent/descent 无关(见 line_layout.rs:313)。

**练习 3**:为什么 `TextLayout` 的缓存条件要求「缓存的布局没有被截断过」?

**答案**:截断后的布局尺寸是「截断文本的尺寸」而非「完整文本的固有尺寸」。Taffy 在布局过程中会用 MinContent/MaxContent 等无约束探测询问固有尺寸,若把截断后的宽度当固有宽度返回,后续布局会基于一个碰巧的值做决策(源码注释称之为 poison)。所以只要发生过截断,缓存对尺寸探测一律失效。

## 5. 综合实践

**任务**:实现一个「自动换行 + 溢出滚动」的文本块,并用手动换行的结果反推内容高度,与界面实际行为互相印证。这是本讲规格里的实践任务,综合了 wrap 算法、行高公式与样式链路。

**准备**:gpui 的 Cargo.toml 用显式 `[[example]]` 声明示例,新建文件需要改构建配置。为了不动源码仓库的结构,推荐**临时替换** `examples/text_wrapper.rs` 的内容(学习结束后 `git checkout crates/gpui/examples/text_wrapper.rs` 还原);也可以按仓库里任一 `[[example]]` 段的格式自行声明新示例。

以下是完整的示例代码(**示例代码**,非项目原有内容),替换 `examples/text_wrapper.rs` 后运行 `cargo run -p gpui --example text_wrapper`:

```rust
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{App, Bounds, Context, Window, WindowBounds, WindowOptions,
           div, font, prelude::*, px, size};
use gpui_platform::application;

struct WrappedBlock {
    text: &'static str,
}

impl Render for WrappedBlock {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        // ── 第一部分:估算路径的手动换行,算出「预期的内容高度」──
        let font = font(".ZedMono");           // 等宽字体,宽度好估
        let font_size = px(16.);
        let line_height = px(24.);             // 1.5 倍行距,和下方样式一致

        // 从池里借一个 wrapper(实践 4.3 的入口)
        let mut wrapper = cx.text_system().line_wrapper(font, font_size);

        // 用一个固定宽度手动换行,数一数断点数
        let probe_width = px(400.);
        let boundaries: Vec<_> = wrapper
            .wrap_line(&[gpui::LineFragment::text(self.text)], probe_width)
            .collect();
        let visual_lines = boundaries.len() + 1;             // 行数 = 断点数 + 1
        let expected_height = line_height * visual_lines as f32;  // 行高公式

        // ── 第二部分:真正的 UI,交给 GPUI 自动换行 ──
        div()
            .id("page")
            .size_full()
            .flex()
            .flex_col()
            .p_4()
            .gap_2()
            .bg(gpui::white())
            .child(
                div()
                    .text_xl()
                    .text_color(gpui::rgb(0x3355aa))
                    .child(format!(
                        "手动测量:宽度 {:.0}px 内约 {} 行,预期内容高度 {:.0}px",
                        probe_width.0, visual_lines, expected_height.0
                    )),
            )
            .child(
                // 外层固定高度 + 垂直滚动:内容高度超过 300px 时出现滚动条
                div()
                    .id("scroll")
                    .h(px(300.))
                    .w(probe_width)
                    .overflow_y_scroll()
                    .border_1()
                    .border_color(gpui::red())
                    .child(
                        // 内层不设高度:由文本的测量结果自然撑开
                        div()
                            .font_family(".ZedMono")
                            .text_size(px(16.))
                            .line_height(gpui::relative(1.5))
                            .child(self.text),
                    ),
            )
            .child(div().text_sm().child(
                "拖动窗口边缘改变宽度,观察下方滚动条与重新换行",
            ))
            .child(
                div()
                    .w(probe_width)
                    .text_sm()
                    .line_clamp(3)
                    .border_1()
                    .border_color(gpui::blue())
                    .child(self.text),
            )
    }
}

fn run_example() {
    application().run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(800.0), px(600.0)), cx);
        cx.open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                ..Default::default()
            },
            |_, cx| {
                cx.new(|_| WrappedBlock {
                    text: "The quick brown fox jumps over the lazy dog. \
                           敏捷的棕色狐狸跳过了懒惰的狗,a-b、var_name 与 \
                           https://github.com/zed-industries/zed 都在白名单里,\
                           但 CJK 字符两两之间都可以断行。",
                })
            },
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

**操作步骤**:

1. 保存后运行 `cargo run -p gpui --example text_wrapper`(编译与运行结果**待本地验证**)。代码里用到的 `gpui::LineFragment`、`gpui::font` 均经 `line_wrapper.rs → text_system.rs → gpui.rs` 的两级 `pub use ... *` 重导出(u1-l3 讲过的「重导出电梯」),可直接从 crate 根引用。
2. 记下顶部显示的「预期内容高度」与手动断点数。
3. **拖动窗口边缘**:观察中间红色边框区域——内容重新换行、行数变化;当内容高度超过 300px 时出现垂直滚动条。
4. 对比蓝色边框的 `line_clamp(3)` 块:最多三行、末尾省略号,验证 4.2 的 `truncate_wrapped_line`。

**需要观察的现象**:

- 窗口越窄,手动测量区的断点数越多,预期高度越大;滚动区实际滚动范围同步变化。
- 中文部分在任意两字之间断行,`a-b`、`var_name`、URL 的 `/` 处也可断,但 `var_name` 不在 `_` 处断——4.2 白名单的直接可视化。
- 固定 400px 宽的手动换行结果与滚动区在同样宽度下的实际断行位置**基本一致**(估算与整形路径共享同一套词边界规则),但边界处可能差一个字符——这就是「无 kerning 的估算」与「真实整形」的差距。

**预期结果**:你同时用两条路径(手动 `wrap_line` 与样式驱动的自动换行)处理了同一段文本,验证了「断点数 + 1」× 行高的高度公式,并亲眼看到池化句柄 `line_wrapper()` 的调用方式。

## 6. 本讲小结

- GPUI 有**两条换行路径**:整形前用 `LineWrapper::wrap_line` 逐字符估算(服务于截断,产出字节下标 `Boundary`);整形后用 `LineLayout::compute_wrap_boundaries` 按字形 x 坐标精确计算(服务于渲染,产出 `WrapBoundary`)。两者共享 `is_word_char` 词边界白名单,其余逻辑是源码注释明说的一次「有意的复制」。
- `wrap_line` 是**贪心 + 回退**:超宽时优先回退到最近的安全断点(空格后的词首、或任意非空白 CJK 字符),没有安全断点才硬断;首行前导空格转化为悬挂缩进(`next_indent`,上限 256)。
- 词字符白名单 `is_word_char` 决定什么不会被拆开:拼音文字字母、粘性符号(`- _ . @ #` 等)、不换行粘合字符;CJK 与 `/`、`\` 不在其中,允许断行。
- `LineWrapperHandle` 把 wrapper 按 `(FontId, 字号)` **池化复用**:借出用 `pop`,Drop 推回,配合 `LineWrapper` 内部的 ASCII 定长数组 + HashMap 两级字符宽度缓存,把「单字符一次完整整形」的昂贵成本摊薄到近零。
- 截断家族(`truncate_line` / `truncate_wrapped_line`)复用同一套宽度缓存;后者逐字符复刻换行状态机以精确处理 `line_clamp`,且 elements/text.rs 侧有「先整形量真实宽度防误截」的补丁——因为逐字符累加高估宽度。
- **行高公式**:内容高度 = 行高 × (断点数 + 1),与 ascent/descent 无关;绘制时 `WrappedLine::paint` 遍历字形、命中断点即折行;对齐宽度取 `wrap_width` 而非文本自然宽度。

## 7. 下一步学习建议

- **u6-l7(图片、SVG 与资源缓存)**:继续「内容元素」这一线索,看非文本内容如何加载、缓存与渲染,与本文的 `LineFragment::Element`(行内固定宽度元素)遥相呼应。
- 想深入 Zed 编辑器如何用 `LineWrapper` 做真实代码软换行的读者,可以在仓库根的 `crates/editor` 里搜索 `line_wrapper` 的调用点,观察它如何把 `Boundary` 翻译成编辑器的 display row——那是本讲估算路径最大的真实消费者。
- 回头重读 [line_layout.rs:227-228](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/text_system/line_layout.rs#L227-L228) 那句「复制了一份代码」的注释,思考:如果让你消除这次复制(把断行算法参数化成「宽度来源」可插拔),接口该怎么设计?这是一个很好的源码阅读收尾练习。
