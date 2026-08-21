# 内置元素一览：text、img、svg、canvas

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 GPUI 文本 API 的四个层级（字符串直接当 child、`text!` 宏、`StyledText`、`InteractiveText`），并知道什么时候用哪一层。
2. 掌握 `img()` 的四种图片来源（`ImageSource` 的四个变体）、`StyledImage` 提供的图片样式（灰度、`object_fit`、加载态、失败回退）。
3. 掌握 `svg()` 的三种数据供给方式（`path` / `external_path` / `data`）、它依赖 `text_color` 上色的特殊行为，以及 `Transformation` 变换。
4. 理解 `canvas()` 作为「命令式绘制逃生舱」的设计：两个只执行一次的回调如何映射到元素生命周期。
5. 理解 `SharedString` 与 `SharedUri` 这两个字符串 newtype 各自的引入动机。
6. 面对「显示一段内容」的需求时，优先选用内置元素，而不是立刻动手写自定义 `Element`（自定义元素是第 4 单元 u4-l1 的主题）。

## 2. 前置知识

本讲建立在 u3-l2（div 与样式 API）和 u3-l3（Style 与 StyleRefinement）之上，先帮你把几个旧概念串起来，再补两个新概念。

**容器元素与内容元素。** `div` 是容器：它擅长布局（flexbox）、背景、边框、交互，但它自己不「画内容」。真正把文字、图片、矢量图形放进布局的是内容元素。本讲的四个主角 `StyledText`、`Img`、`Svg`、`Canvas` 都属于内容元素，它们全都实现了 `Element` trait，因此都能直接塞进 `div().child(...)`。

**元素三阶段的直觉版。** 虽然要到 u4-l1 才逐方法精读 `Element` trait，但读本讲的源码需要先有这个直觉：每个元素每帧都要走三个阶段——

- `request_layout`：向布局引擎申报「我多大」，同时往往要完成测量（文本 shaping、图片尺寸获取）；
- `prepaint`：拿到布局分派给自己的 `bounds`，做绘制前的准备（记录位置、创建命中盒）；
- `paint`：真正向场景提交绘制指令。

本讲会反复看到内置元素如何在这三个阶段里搬运状态。

**SharedString（复习）。** `SharedString` 是一个不可变、克隆成本极低的字符串句柄——文档自述「本质上是 `Arc<str>` 与 `&'static str` 的抽象，当前由 `SmolStr` 支撑」（定义在独立的 `gpui_shared_string` crate，经 [src/gpui.rs:137](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/gpui.rs#L137) 的 `pub use` 进入 `gpui` 命名空间）。动机是「字符串要么编译期就定死、要么共享所有权，克隆永远不复制内容」。UI 每帧重建元素树，字符串句柄来回传递时零拷贝就非常重要。

**AssetSource（新面孔，一句话版）。** 应用可以把资源目录挂进 GPUI（`application().with_assets(...)`），之后用相对路径字符串（如 `"image/app-icon.png"`）就能取到字节。资源加载与缓存体系的完整讲解在 u6-l7，本讲只需要知道「字符串路径 → 异步拿到字节」这条通路存在。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/elements/text.rs`（约 1300 行） | 文本元素全家：`Text`、`text!` 宏、`StyledText`、`TextLayout`（测量与缓存）、`InteractiveText`，以及 `&str`/`String`/`SharedString` 直接作为元素的实现 |
| `src/elements/img.rs`（约 700 行） | `Img` 元素、`ImageSource` 四种来源、`ImageStyle`/`StyledImage` 图片样式、图片资源加载器 |
| `src/elements/svg.rs`（约 300 行） | `Svg` 元素、`Transformation` 变换、外部路径直读的 `SvgAsset` |
| `src/elements/canvas.rs`（不到 100 行） | `Canvas` 元素：用两个回调访问底层绘制 API 的最短路径 |
| `src/shared_uri.rs` | `SharedUri`：标记「这段字符串是 URI」的 newtype |
| `src/asset_cache.rs` | `Resource` 枚举（Uri/Path/Embedded）与 `Asset` trait，图片加载的底层词汇 |
| `src/style.rs`（节选） | `ObjectFit` 枚举、`HighlightStyle` 高亮样式 |
| `examples/text_layout.rs` | `StyledText::with_highlights` 的最小可运行用法 |
| `examples/text.rs` | 字体注册与字号阶梯的排版样本页 |
| `examples/image/image.rs` | `img()` 三种来源（本地路径 / 远程 URI / 内嵌资产）对照展示 |
| `examples/svg/svg.rs` | `svg().path()` 用 `AssetSource` 加载并按 `text_color` 重新着色 |
| `examples/painting.rs` | `canvas()` 承载自由绘制（画笔、虚线、清除） |

## 4. 核心概念与源码讲解

### 4.1 文本元素：字符串、text!、StyledText 与 InteractiveText

#### 4.1.1 概念说明

GPUI 的文本 API 是一个四层阶梯，从简到繁：

1. **直接把字符串当 child**：`div().child("hello")`。`&'static str`、`String`、`Cow<'static, str>`、`SharedString` 都实现了 `IntoElement`，样式完全继承自父元素的 `text_style`（u3-l3 讲过文本样式沿树级联）。这是 90% 场景的正确选择。
2. **`text!` 宏**：生成一个带 `ElementId` 的 `Text` 元素。ID 的作用不是交互，而是无障碍树——屏幕阅读器靠 ID 判断「文本内容更新了」还是「旧节点销毁、新节点诞生」。宏会用源码位置（文件名+行号+列号）自动生成稳定 ID。
3. **`StyledText`**：一段文本里混多种样式（加粗一段、斜体一段、不同颜色一段）。它把文本切成若干 `TextRun`（每 run 一个样式），这也是语法高亮渲染的基础形态。
4. **`InteractiveText`**：在 `StyledText` 外再包一层，让某些字符区间可点击、可悬停、可弹 tooltip——链接、行内提示就靠它。

为什么分层？因为样式统一的文本根本不需要构造开销更大的 `StyledText`，而 `Text` 的 ID 语义对无障碍又不可或缺——按需付费，各取所需。

#### 4.1.2 核心流程

以 `StyledText` 为例，一帧内的流程是：

```text
构造期（render 中）:
  StyledText::new(text)
    .with_highlights([(range, style), ...])   ← 只是暂存，不计算
        │
        ▼ 布局阶段 request_layout
  1. 若未显式给 runs：用 window.text_style()（继承来的文本样式）
     作为默认样式，把 highlights 切成 TextRun 序列（compute_runs）
     —— 这就是「延迟计算」：构造时还不知道继承样式，布局时才知道
  2. TextLayout::layout() 调用文本系统 shape_text 完成排版测量，
     结果（行、宽度、行高）缓存进 Rc<RefCell<Option<...>>>
  3. 缓存命中条件满足时直接返回上次尺寸，跳过 shaping
        │
        ▼ 预绘制阶段 prepaint
  记录本帧分配到的 bounds（没有它 paint 会 panic）
        │
        ▼ 绘制阶段 paint
  逐行调用 line.paint_background（画高亮背景）与 line.paint（画字形）
```

`TextRun` 的切分规则（`compute_runs`）：高亮区间按顺序排列，区间之间的「空隙」用默认样式补一个 run，区间内的 run 是默认样式叠加高亮。区间端点必须落在 UTF-8 字符边界上，debug 构建下有断言检查。

#### 4.1.3 源码精读

**第一层：字符串直接当元素。** `&'static str` 和 `SharedString` 各自实现了 `Element`，两段实现高度相似——`request_layout` 里新建一个 `TextLayout` 并测量，`prepaint` 记录 bounds，`paint` 绘制：

[examples/text_layout.rs:20-22](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/text_layout.rs#L20-L22) 这三行 `div().child("Text left")` 就是把 `&'static str` 直接当 child 用的实例。

[examples/text_layout.rs:78-82](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/text_layout.rs#L78-L82) 则是第三层 `StyledText` 的最小用法——`with_highlights` 接收 `(字节区间, HighlightStyle)` 列表，`FontWeight::EXTRA_BOLD.into()` 和 `FontStyle::Italic.into()` 说明 `HighlightStyle` 为常用样式值实现了 `From` 转换。

`Text` 结构本身只有两个字段——可选 ID 加文本：

[src/elements/text.rs:66-70](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L66-L70) `Text { id: Option<ElementId>, text: SharedString }`。注意它的三阶段实现全部委托给内部 `SharedString` 的 `Element` 实现（[src/elements/text.rs:201-209](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L201-L209)），`Text` 相比裸字符串只多贡献了一件事：ID 带来的无障碍语义（`a11y_role` 返回 `Label`）。

**`text!` 宏的 ID 生成。**

[src/elements/text.rs:159-167](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L159-L167) 不带 `id =` 时，宏用 `file!()/line!()/column!()` 拼出源码位置并哈希成 `ElementId::Integer`。同一次宏调用（同一行）生成的 ID 稳定，两处不同调用生成的 ID 不同——文档注释里专门强调了这一点对屏幕阅读器播报的影响。

**`StyledText` 的字段与延迟计算。**

[src/elements/text.rs:391-397](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L391-L397) `runs`、`delayed_highlights`、`delayed_font_family_overrides` 三个字段都是 `Option`——构造时要么直接给 runs，要么暂存高亮等待布局期合成，两者互斥（`with_default_highlights` 与 `with_highlights` 各有 debug 断言禁止混用，见 [src/elements/text.rs:418-451](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L418-L451)）。

[src/elements/text.rs:554-575](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L554-L575) `request_layout` 里的合成顺序：先取现成 runs，没有则用 `window.text_style()`（此处才拿到继承样式！）对 `delayed_highlights` 调 `compute_runs`；再叠加字体族覆盖；最后交给 `TextLayout::layout`。

[src/elements/text.rs:453-478](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L453-L478) `compute_runs` 的切分循环：空隙补默认 run、区间做 `default_style.clone().highlight(highlight).to_run(...)`。

**`TextLayout`：测量结果的跨帧缓存。**

[src/elements/text.rs:612-624](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L612-L624) 内部是 `Rc<RefCell<Option<TextLayoutInner>>>`，`TextLayoutInner` 缓存了行列表、行高、换行宽度、尺寸与 bounds。元素每帧重建，但这个 `Rc` 可以被调用方持有（`StyledText::layout()` 还把它公开出来），于是相同文本、相同换行宽度时能直接复用上次的测量结果——缓存命中条件（尺寸已缓存、wrap 宽度一致、未发生截断）见 [src/elements/text.rs:679-694](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L679-L694)。

[src/elements/text.rs:738-747](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L738-L747) 真正的排版发生在 `window.text_system().shape_text(...)`——第四个参数是换行宽度、第五个是 `line_clamp`（最大行数），这正是 u6-l5 文本系统要深入的地方。

[src/elements/text.rs:830-861](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L830-L861) `index_for_position`（像素坐标 → 字节下标）与 [src/elements/text.rs:864-892](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L864-L892) 的 `position_for_index`（反向）是编辑器「点击定位光标」「光标反查像素」的基础能力，`InteractiveText` 的点击判定也建立在它之上。

**`InteractiveText` 的构造参数。**

[src/elements/text.rs:1006-1056](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L1006-L1056) 构造需要 `ElementId` 加一个 `StyledText`；`on_click` 接收「可点击区间列表」，回调参数是被命中的**区间下标**（不是字符下标）；`on_hover` 回调拿到悬停字符下标或 `None`；`tooltip` 用闭包按字符下标构建提示视图。

#### 4.1.4 代码实践

**实践目标**：用最小改动体会「高亮区间 → TextRun 切分」的规则，以及 `HighlightStyle` 的字段构成。

**操作步骤**：

1. 打开 `examples/text_layout.rs`，找到第 78-82 行的 `StyledText::new("ABCD").with_highlights([...])`。
2. 把区间改成覆盖中间两个字符的加粗加颜色：
   ```rust
   .child(div().flex().gap_2().justify_between().child(
       StyledText::new("ABCD").with_highlights([
           (0..2, FontStyle::Italic.into()),
           (2..4, rgb(0xcc0000).into()),   // From<Rgba> for HighlightStyle
       ]),
   ))
   ```
   （此为示例代码，基于原文件修改。）
3. 运行：`cargo run -p gpui --example text_layout`。
4. 再打开 [src/style.rs:580-598](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs#L580-L598) 对照 `HighlightStyle` 的完整字段表：`color`、`font_weight`、`font_style`、`background_color`、`underline`、`strikethrough`。

**需要观察的现象**：AB 呈斜体、CD 呈红色；两个区间首尾相接处没有缝隙也没有重叠。

**预期结果**：四个字符各自样式正确；如果把区间写成 `(0..1)` 和 `(2..4)`，中间的 B 会回落到继承样式。若把区间端点写进一个多字节字符内部（把文本换成含中文的字符串再切半个字符），debug 构建会触发 `is_char_boundary` 断言 panic——这验证了「区间必须落在字符边界」的规则。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `with_highlights` 不在构造时立刻计算 runs，而要拖到 `request_layout`？

**参考答案**：构造 `StyledText` 发生在 `render()` 里，那时元素还没有进入元素树，拿不到最终继承到的 `TextStyle`（它取决于父级 div 的文本样式栈，u3-l3 讲过 `window.text_style()` 从栈中折叠而来）。延迟到布局期才能用真实的默认样式去合成 runs；`delayed_highlights` 这个字段名就是明证。

**练习 2**：`text!("hello")` 和 `Text::new_inaccessible("hello")` 有什么区别？各自适合什么场景？

**参考答案**：前者带基于源码位置的稳定 ID，会进入无障碍树（`a11y_role` 返回 `Label`，值随文本变化通知屏幕阅读器），适合面向用户的主要文本；后者没有 ID，不产生无障碍节点，适合自定义组件内部的装饰性文字（由父容器统一标注无障碍属性），能减少无障碍树噪音。

**练习 3**：想实现「点击文本中的链接字符区间执行跳转」，应该用哪层 API？回调里拿到的是什么？

**参考答案**：`InteractiveText::new(id, styled_text).on_click(vec![link_range], |range_ix, window, cx| ...)`。回调第一个参数是被点击的**区间下标**（`clickable_ranges` 中的位置），不是字符位置——源码里 `on_click` 把命中判定为「mouse_down 与 mouse_up 都落在同一区间」后才调用你的闭包并传入该区间下标。

### 4.2 图片元素 Img：四种来源与一套图片样式

#### 4.2.1 概念说明

`img(source)` 一个函数完成构造，但 `source` 的类型 `ImageSource` 是个四选一枚举，这是理解 `Img` 的钥匙：

| 变体 | 来源 | 典型用法 |
| --- | --- | --- |
| `Resource(Resource::Uri)` | 远程 URI | `img("https://picsum.photos/800/400")` |
| `Resource(Resource::Embedded)` | 应用内嵌资产（相对路径走 `AssetSource`） | `img("image/app-icon.png")` |
| `Resource(Resource::Path)` | 文件系统绝对/相对路径 | `img(Path::new("..."))` |
| `Render(Arc<RenderImage>)` | 已经解码好的像素数据 | 程序生成的位图 |
| `Image(Arc<Image>)` | 原始字节（可含 SVG），到布局期才解码 | 测试与预处理 |
| `Custom(闭包)` | 完全自定义的加载函数 | 相册缩略图等自管缓存 |

（`Resource` 本身是 Uri/Path/Embedded 三选一，见 [src/asset_cache.rs:11-19](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/asset_cache.rs#L11-L19)。）

给 `&str` 时如何区分 Uri 和 Embedded？——尝试按 URL 解析，解析成功就是 Uri，否则当作内嵌资产路径。

`StyledImage` trait 则提供图片专属样式：`grayscale(bool)` 灰度、`object_fit(ObjectFit)` 填充策略（默认 `Contain`）、`with_loading(闭包)` 加载中的替身元素、`with_fallback(闭包)` 加载失败的替身元素。注意后两者接收的是「返回 `AnyElement` 的闭包」——加载态和失败态本身也是任意元素树。

`Img` 同时实现了 `Styled` 和 `InteractiveElement`，所以 div 的尺寸方法（`.size_8()`、`.w(px(180.))`）和交互方法它全都能用。

#### 4.2.2 核心流程

`Img` 一帧的生命周期比文本元素复杂，因为要处理「数据还没到」和「多帧动图」两种时态：

```text
构造: img(source).size(...)           ← 仍是普通数据，未加载
  │
  ▼ request_layout
use_data() 向缓存/资产系统要数据:
  ├─ Some(Ok(data))     → 已有像素:
  │     · 动图且窗口活跃且未开 reduce_motion → 按帧延迟推进 frame_index，
  │       并 request_animation_frame() 请求下一帧
  │     · style.aspect_ratio 未设 → 用图片宽高比补上
  │     · 宽或高为 Auto → 按已知一边+宽高比推另一边
  ├─ Some(Err)          → 加载失败: 若配了 with_fallback，
  │                        测量替身元素顶替本元素
  └─ None               → 尚未加载完成: 若超过 LOADING_DELAY(200ms)
                           且配了 with_loading，测量加载态替身；
                           否则起一个 200ms 定时器，到点 notify 重排
  │
  ▼ prepaint: 走 Interactivity（样式+命中盒），替身元素若有则 prepaint
  ▼ paint:   再次 use_data():
             object_fit.get_bounds() 计算图片在元素框内的实际位置
             → window.paint_image(...) 上屏；失败/加载中则画替身
```

#### 4.2.3 源码精读

**`ImageSource` 枚举与字符串判定。**

[src/elements/img.rs:40-51](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L40-L51) 四个变体的定义；`Custom` 变体直接装一个 `(Window, App) -> Option<Result<...>>` 闭包。

[src/elements/img.rs:63-71](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L63-L71) `From<&str>` 的实现：`is_uri(s)` 成功走 `Resource::Uri`，失败走 `Resource::Embedded`。所以同一个字符串 `img("image/app-icon.png")` 与 `img("https://.../x.png")` 走的是两条完全不同的加载通路（前者问 `AssetSource`，后者发 HTTP 请求）。

[src/elements/img.rs:89-105](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L89-L105) `From<&Path>`/`From<PathBuf>`/`From<Arc<Path>>` 全部映射到 `Resource::Path`——这就是「本地图」的入口。

**`StyledImage` 图片样式 trait。**

[src/elements/img.rs:147-177](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L147-L177) 五个方法，全部读写 `ImageStyle`；`ImageStyle` 的默认值（`object_fit: Contain`）见 [src/elements/img.rs:136-145](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L136-L145)。

**加载延迟与替身。**

[src/elements/img.rs:31-32](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L31-L32) `LOADING_DELAY = 200ms`：数据未到时先不显示任何加载态，超过 200ms 仍未到才渲染 `with_loading` 提供的替身——避免快速加载时加载闪烁。[src/elements/img.rs:400-423](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L400-L423) 是这段逻辑：起定时器、到点 `cx.notify(current_view)` 触发重排。

**自动宽高比与自动尺寸。**

[src/elements/img.rs:348-380](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L348-L380) 拿到像素数据后：`style.aspect_ratio` 未设置就补图片宽高比；宽为 Auto 时若高已知则按比例推出宽，反之亦然。这解释了 [examples/image/image.rs:122-131](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/image/image.rs#L122-L131) 里 `.h(px(180.))` 单边约束能自动得到另一边的原因。

**动图帧推进。**

[src/elements/img.rs:316-345](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L316-L345) GIF/WebP 的帧序号与每帧延迟由 `ImgState`（跨帧元素状态）记录，条件「窗口活跃 + 未开 reduce_motion」满足时在 [src/elements/img.rs:382-388](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L382-L388) 请求动画帧。动画的驱动者是布局阶段自己。

**绘制与 `ObjectFit`。**

[src/elements/img.rs:490-504](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L490-L504) paint 阶段先用 `object_fit.get_bounds(bounds, 图片尺寸)` 算出图片实际绘制框，再调 `window.paint_image`（圆角来自样式 `corner_radii`）。

[src/style.rs:28-40](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/style.rs#L28-L40) `ObjectFit` 五个变体：`Fill`（拉伸填满）、`Contain`（完整放入留白）、`Cover`（覆盖裁切）、`ScaleDown`（只缩不放）、`None`（原始尺寸），与 CSS `object-fit` 一一对应。

**数据获取的统一入口。**

[src/elements/img.rs:535-554](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L535-L554) `use_data` 按变体分发：`Resource` 优先问 `ImageCache`（没有则走资产系统 `ImgResourceLoader`）、`Custom` 调闭包、`Render` 直接返回、`Image` 走解码器。返回 `Option<Result<...>>`：`None` 表示仍在加载、`Some(Err)` 表示失败、`Some(Ok)` 才有像素——正好对应上面流程图的三个分支。

#### 4.2.4 代码实践

**实践目标**：直观区分 `ObjectFit` 各变体，并验证「自动宽高比」行为。

**操作步骤**：

1. 运行图片示例：`cargo run -p gpui --example image`（该示例在 [Cargo.toml:185-187](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/Cargo.toml#L185-L187) 声明，target 名为 `image`，源码在 `examples/image/image.rs`）。
2. 观察三张并排图（本地文件、远程 URI、内嵌资产）——它们的构造差异在 [examples/image/image.rs:92-113](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/image/image.rs#L92-L113)：`local_resource` 是 `Arc<Path>`、`remote_resource` 是 `SharedUri`、`asset_resource` 是 `SharedString`。
3. 复制 `examples/image/image.rs` 中 `.child(img("https://picsum.photos/800/400").h(px(180.)))` 一行四次，分别加上 `.object_fit(ObjectFit::Fill)`、`Contain`、`Cover`、`ScaleDown`，并把外层 div 固定为 `.size(px(180.))` 的正方形（示例代码，需 `use gpui::ObjectFit;`）。
4. 断网重跑，观察远程图位置发生了什么。

**需要观察的现象**：同一张 800×400 的横图放进 180×180 的框：`Fill` 拉伸变形、`Contain` 完整显示左右留白、`Cover` 铺满上下被裁、`ScaleDown` 表现同 `Contain`（图大于框时缩放）；单边约束 `.h(px(180.))` 的图自动得到 360 宽。

**预期结果**：四个变体行为与上表一致。断网后远程图位置：加载超过 200ms 后显示空（本示例未配置 `with_loading`/`with_fallback`，故什么都不画）。「断网时显示替身」这一步待本地验证（取决于网络环境与 HTTP 客户端配置）。

#### 4.2.5 小练习与答案

**练习 1**：`img("image/app-icon.png")` 和 `img(Path::new("examples/image/app-icon.png"))` 有什么区别？

**参考答案**：前者经 `From<&str>` 判定不是 URI，成为 `Resource::Embedded`，字节来自 `application().with_assets(...)` 挂载的 `AssetSource`，路径是「资产逻辑路径」；后者成为 `Resource::Path`，直接用文件系统路径 `fs::read`。前者把资源来源抽象掉（可以打进二进制、来自网络包），后者直读磁盘。

**练习 2**：为什么 `LOADING_DELAY` 要设 200ms 而不是立即显示 loading 态？

**参考答案**：避免加载闪烁。本地资产和缓存命中的加载通常远快于 200ms，如果立即渲染 loading 元素，会出现「闪一下加载态又变成本体」的抖动；只有慢加载（远程大图）才值得展示加载态。这是一个典型的「延迟显示骨架屏」交互模式。

**练习 3**：GIF 动画的下一帧是谁负责触发的？

**参考答案**：`Img` 的 `request_layout` 自己——帧延迟到期后递增 `frame_index` 并调用 `window.request_animation_frame()` 请求下一帧；同时受 `window.is_window_active()` 与 `cx.reduce_motion()` 双重开关控制（窗口失焦或系统要求减弱动画时停摆，见 [src/elements/img.rs:321-343](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/img.rs#L321-L343)）。

### 4.3 SVG 元素：路径、内联数据与变换

#### 4.3.1 概念说明

`svg()` 与 `img()` 构造方式类似，但有三个独有设计：

1. **三种给字节的方式**：`.path(...)` 走 `AssetSource`（应用内嵌资产）；`.external_path(...)` 绕开资产系统直接读文件系统；`.data(&[u8])` 直接给内联 SVG 字节（用内容哈希当缓存键）。三者互斥，按优先级依次尝试。
2. **颜色来自 `text_color`**：SVG 中的 `currentColor` 会被渲染成元素的文字颜色。这意味着**不设置 `.text_color(...)` 的 svg 不会绘制任何东西**（源码里三个分支都要 `zip(style.text.color)`）——这是初学者最常见的「SVG 不显示」原因。
3. **`Transformation` 变换**：绘制期的缩放/平移/旋转，明确不影响布局与命中盒。

`Svg` 同样实现 `Styled` 与 `InteractiveElement`，尺寸控制（`.size_8()` 等）与 div 完全一致。

#### 4.3.2 核心流程

```text
构造: svg().path("icons/check.svg").text_color(rgb(0x00ff00)).size_4()
  │
  ▼ request_layout / prepaint: 与 img 相同的 Interactivity 流程
  │                            （样式测量、命中盒）
  ▼ paint:
  按 data → external_path → path 的顺序取第一个已配置的来源:
    · data:          直接 paint_svg(bytes)
    · external_path: window.use_asset::<SvgAsset>(path)
                     → SvgAsset::load 用 fs::read 读文件
    · path:          paint_svg(path, None)
                     → SvgRenderer 经 AssetSource 取字节并栅格化
  颜色统一取 style.text.color（即 .text_color 设置值）
```

#### 4.3.3 源码精读

**`Svg` 结构与三种来源。**

[src/elements/svg.rs:15-23](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/svg.rs#L15-L23) 六个字段：`interactivity`（样式与交互的标配容器，u3-l2 讲过 div 也用它）、`transformation`、`path`/`external_path`/`data`/`data_path` 四个来源字段。

[src/elements/svg.rs:39-69](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/svg.rs#L39-L69) 三个来源方法与 `with_transformation`。注意 `.data()` 的实现（L53-62）：对字节做哈希拼成 `__binary_svg__{hash}` 形式的伪路径，让内联数据也能命中同一套按路径索引的 SVG 栅格化缓存——不动缓存结构就接上了新来源，这是个值得学习的手法。

**paint 的三分支与颜色依赖。**

[src/elements/svg.rs:140-187](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/svg.rs#L140-L187) 先把 `transformation` 换算成矩阵（未设置则单位矩阵），然后三分支依次检查 `data`、`external_path`、`path`，每个分支都是「来源 `zip` `style.text.color`」——颜色缺失则整个分支跳过。

**`external_path` 的加载器。**

[src/elements/svg.rs:287-303](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/svg.rs#L287-303) `SvgAsset` 实现了 `Asset` trait（`load` 就是一次 `fs::read`），配合 [src/asset_cache.rs:39-52](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/asset_cache.rs#L39-L52) 的 trait 定义看：`Source` 是缓存键、`Output` 是结果、`load` 返回可 `Send` 的 future——资产系统的全部契约就这三个关联项。

**`Transformation` 的组合。**

[src/elements/svg.rs:212-285](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/svg.rs#L212-L285) `scale`/`translate`/`rotate` 三个构造器加三个 `with_*` 链式补丁；`into_matrix`（L277-284）按「平移到中心 → 旋转 → 缩放 → 平移回去」的顺序复合成变换矩阵。

**示例对照。**

[examples/svg/svg.rs:42-71](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/svg/svg.rs#L42-L71) 同一个 `svg/dragon.svg` 用三种 `text_color`（红绿蓝）渲染三份——验证了「一个 SVG 文件 + 任意着色」的图标方案；其 `Assets` 实现（[examples/svg/svg.rs:13-38](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/svg/svg.rs#L13-L38)）和挂载方式（[examples/svg/svg.rs:73-78](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/svg/svg.rs#L73-L78)）就是 `AssetSource` 的最小实现模板。

#### 4.3.4 代码实践

**实践目标**：验证「SVG 颜色来自 `text_color`」与「不设颜色不绘制」两个行为。

**操作步骤**：

1. 运行：`cargo run -p gpui --example svg`，观察红绿蓝三条龙。
2. 复制其中一个 child，删掉 `.text_color(...)`：
   ```rust
   .child(svg().path("svg/dragon.svg").size_8())   // 示例代码：无 text_color
   ```
3. 再给一个 child 加上旋转变换：
   ```rust
   .child(
       svg()
           .path("svg/dragon.svg")
           .size_8()
           .text_color(rgb(0xff00ff))
           .with_transformation(
               Transformation::rotate(0.5)
                   .with_scaling(size(1.5, 1.5)),
           ),
   )
   ```
   （`Transformation` 与 `size` 均来自 prelude / `gpui::*`。）

**需要观察的现象**：第 2 步的 SVG 完全消失（布局上仍占位吗？——注意观察其他元素是否移动）；第 3 步的 SVG 呈放大且旋转的洋红色。

**预期结果**：无 `text_color` 的 SVG 不绘制任何内容；带变换的 SVG 视觉上旋转缩放，但元素占位（布局与命中盒）不变。布局是否占位这一点待本地验证（取决于样式是否给出固定尺寸——`.size_8()` 申报了尺寸，布局框应存在，仅内容不画）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GPUI 选 `text_color`（继承自文本样式栈）而不是单独的 `.color(...)` 方法给 SVG 上色？

**参考答案**：让图标颜色跟随上下文「免费」继承。SVG 用 `currentColor` 表达可着色区域，元素只需读 `style.text.color`；于是给一个 div 设 `.text_color(...)`，里面的所有 svg 与文字统一定色，换主题时不需要逐个图标改颜色。代价是：忘记设颜色时静默不绘制。

**练习 2**：`.path(...)`、`.external_path(...)`、`.data(...)` 各适合什么场景？

**参考答案**：`.path` 适合应用自带的图标库（经 `AssetSource`，可打包进二进制、支持 wasm）；`.external_path` 适合用户提供的文件（主题目录、插件资产），绕开资产抽象直接读磁盘；`.data` 适合运行时生成/网络下发的 SVG 字节，用内容哈希当缓存键避免重复栅格化。

**练习 3**：`with_transformation` 的文档说「不影响 hitbox 或布局」，从源码哪里能印证？

**参考答案**：`Transformation` 只在 paint 阶段被消费——[src/elements/svg.rs:140-147](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/svg.rs#L140-L147) 中 `into_matrix` 的调用位于 `interactivity.paint(...)` 的绘制闭包内，而 `request_layout`（L84-99）与 `prepaint`（L101-119）完全不读取 `self.transformation`。

### 4.4 Canvas：命令式绘制的逃生舱

#### 4.4.1 概念说明

`canvas(prepaint, paint)` 用两个回调把 GPUI 的底层绘制 API（`window.paint_quad`、`window.paint_path` 等）直接暴露给你，省去手写一个完整的自定义 `Element`。文档自称「短期自定义绘制的捷径」。

它最特别的设计是**泛型参数 `T`**：`prepaint` 回调返回一个 `T`，`paint` 回调接收这个 `T`——这是给「两阶段之间传状态」预留的通道（比如 prepaint 算好的插值参数、缓存句柄）。

两个回调都是 `FnOnce` 且在首次调用后被 `take()` 走：元素树每帧重建（立即模式），canvas 的闭包也是每帧由 `render()` 重新构造的，用一次即弃是自然的选择。也正因为 `Canvas::id()` 恒返回 `None`，它**没有跨帧元素状态**——需要跨帧的数据（如自由绘制的笔迹）必须由闭包外的实体状态（`Rc<RefCell<...>>` 或实体字段）承载。

#### 4.4.2 核心流程

```text
构造: canvas(
    move |bounds, window, cx| -> T { ... },   // prepaint 回调
    move |bounds, state: T, window, cx| { ... }, // paint 回调
).size_full()                                  // Styled 照常可用
  │
  ▼ request_layout: 把自身 StyleRefinement 合成为 Style，申报给布局引擎
  ▼ prepaint:      拿到 bounds，执行 prepaint 回调，返回值存入 PrepaintState
  ▼ paint:         在 style.paint(...) 内执行 paint 回调
                   （style.paint 会先画背景/边框等样式，再调你的闭包）
```

#### 4.4.3 源码精读

整个文件不到 100 行，可以完整读完：

[src/elements/canvas.rs:10-19](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L10-L19) 构造函数签名——两个 `FnOnce` 回调，第一个返回 `T`、第二个接收 `T`。

[src/elements/canvas.rs:22-27](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L22-L27) 结构体只有三个字段：两个 `Option<Box<dyn FnOnce...>>` 加一个 `StyleRefinement`。

[src/elements/canvas.rs:49-60](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L49-L60) `request_layout`：`Style::default()` 出发 `refine(&self.style)` 合成最终样式——这正是 u3-l3 讲的「补丁合成完整样式」的一次现场应用；没有孩子，所以 children 参数传 `[]`。

[src/elements/canvas.rs:62-72](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L62-L72) `prepaint`：`self.prepaint.take().unwrap()(bounds, window, cx)`——`Option` + `take` 是「只执行一次」的实现方式，`PrepaintState = Option<T>` 暂存返回值。

[src/elements/canvas.rs:74-88](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L74-L88) `paint`：在 `style.paint(bounds, window, cx, |window, cx| ...)` 的闭包里执行用户的 paint 回调——外层的 `style.paint` 负责先渲染样式声明的背景、边框、阴影，你的绘制内容叠加其上。

**用法范例。**

[examples/painting.rs:356-398](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/painting.rs#L356-L398) 自由绘制画板：prepaint 回调为空 `move |_, _, _| {}`；paint 回调里先画背景 `quads`，再用 `PathBuilder` 把实体字段 `lines` 中的笔迹逐段连线，`.size_full()` 占满容器；紧随其后的 `.on_mouse_down(...)`（L401 起）挂在**包裹 canvas 的 div** 上，鼠标事件写入实体状态，下一帧 `render()` 重新构造携带新笔迹的闭包——状态在实体、绘制在 canvas，这正是 GPUI 的标准分工。

#### 4.4.4 代码实践

**实践目标**：体验「样式先画、回调后画」的顺序，以及 `T` 通道的用法。

**操作步骤**：

1. 运行：`cargo run -p gpui --example painting`，按住鼠标拖动画线，按住 Shift 画直线，切换 Solid/Dashed 与 Clear。
2. 阅读 [examples/painting.rs:356-398](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/painting.rs#L356-L398)，注意 paint 回调如何依次画三层：背景 quads → 默认路径 → 用户笔迹。
3. 把外层 div 加上 `.bg(rgb(0xffffdd)).border_1().border_color(rgb(0x0000ff))`（示例代码），重跑。
4. 思考实验（不必运行）：若把笔迹从实体字段移进 paint 闭包里的局部 `Vec`，会发生什么？

**需要观察的现象**：第 3 步中背景色与边框出现在 canvas 绘制内容的**下层**——`style.paint` 先渲染样式、用户回调后执行。

**预期结果**：画出的线始终可见（在上层）；第 4 步的答案见下面练习 2。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Canvas` 的两个回调是 `FnOnce` 而不是 `Fn`？

**参考答案**：元素是立即模式的，每帧 `render()` 都会重新构造整个元素树，canvas 的闭包自然每帧新生成、执行一次后随元素一起丢弃。源码用 `Option<Box<dyn FnOnce>>` + `take()` 实现「一次性」；如果用 `Fn` 就得要求闭包借用而非消耗捕获的数据，反而限制可传递的状态。代价是 `Canvas` 不能作为持久对象被复用——需要每帧重建的正是它的工作方式。

**练习 2**：把 `painting.rs` 的笔迹从实体字段移到 paint 闭包内的局部变量，还能画出持续累积的线吗？

**参考答案**：不能。局部 `Vec` 随闭包每帧重建，永远是空的；上一帧的笔迹没有任何地方保存。跨帧状态必须放在闭包之外——示例放在视图实体的 `lines` 字段里，由 `render()` 每帧克隆进新的闭包。对比 `Img` 用 `with_element_state` 存跨帧状态（u4-l1 详讲），`Canvas` 因为没有 ID 而用不了元素状态机制，只能依赖外部存储。

**练习 3**：`canvas()` 的 `T` 泛型通道什么时候有用？

**参考答案**：当 prepaint 阶段计算的中间结果 paint 阶段还要用、且不方便重算时。例如 prepaint 里根据 bounds 做一次坐标采样/插值得到 `T`，paint 直接消费 `T` 绘制；避免了在 paint 里重复计算，也避免了把临时数据泄漏到外部状态。两个回调都不需要传数据时写 `canvas(move |_, _, _| {}, move |_, _, window, _| {...})` 即可（`T = ()`）。

### 4.5 选型指南与两个字符串 newtype 的动机

**内容元素选型表：**

| 需求 | 用什么 |
| --- | --- |
| 统一样式的一段文字 | 直接 `div().child("...")`（字符串实现 `IntoElement`） |
| 需要无障碍稳定 ID 的文字 | `text!(...)` 宏 |
| 一段文字多种样式（高亮、粗斜体混排） | `StyledText::new(...).with_highlights([...])` |
| 文字区间可点击/悬停/带 tooltip | `InteractiveText` |
| 位图、动图、远程图 | `img(source)` + `StyledImage` |
| 矢量图标（可随主题变色） | `svg().path(...).text_color(...)` |
| 少量自定义图形（画线、波形、图表） | `canvas(prepaint, paint)` |
| 复杂自定义布局 + 跨帧元素状态 | 自定义 `Element`（u4-l1） |

经验法则：**先问内置元素能不能胜任**。`canvas` 是「不想写完整 Element」的捷径，但一旦需要元素状态（`.id()`）、复杂命中行为或性能调优，就该升级为自定义 `Element`——第 4 单元会走这条路。

**`SharedString` 与 `SharedUri` 的动机对照：**

- [`SharedString`](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui_shared_string/gpui_shared_string.rs#L11-L15)（定义在 `gpui_shared_string` crate，注意链接已跳出 `crates/gpui` 目录）解决**性能与所有权**问题：它把「编译期常量 `&'static str`」与「共享所有权 `Arc<str>`」两种形态抽象成一个可克隆零拷贝的句柄（当前底层是 `SmolStr`），元素树每帧重建时字符串来回传递不产生复制。
- [`SharedUri`](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/shared_uri.rs#L5-L25)（定义见 [src/shared_uri.rs:5-25](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/shared_uri.rs#L5-L25)，就是 `SharedString` 的 newtype）解决**语义**问题：它只出现在「这串字符确定是 URI」的位置（如 `Resource::Uri(SharedUri)`、`Img` 的远程来源），把「任意文本」和「资源定位符」在类型层面分开，防止把普通路径误传给 HTTP 客户端之类的错用；同时派生 `Hash + Eq`，直接充当资产缓存的键。

两者是「同一个底层类型、两种表达意图」的典范：`SharedString` 说明*怎么存*，`SharedUri` 说明*是什么*。

## 5. 综合实践

**任务**：做一个图文混排卡片——一张本地图（`img()`）、一个 SVG 图标、一段多样式 `StyledText`，全部在一个 div 中对齐排列。这个任务把本讲四个模块中的三个（text、img、svg）串起来，并复习 u3-l2 的布局方法。

**操作步骤**：

1. 新建 `crates/gpui/examples/media_card.rs`（示例代码，完整文件如下）：

   ```rust
   #![cfg_attr(target_family = "wasm", no_main)]

   use std::fs;
   use std::path::PathBuf;

   use anyhow::Result;
   use gpui::{
       App, AssetSource, Bounds, Context, FontStyle, FontWeight, SharedString, StyledText,
       Window, WindowBounds, WindowOptions, div, img, prelude::*, px, rgb, size, svg,
   };
   use gpui_platform::application;

   /// 与 examples/svg、examples/image 相同的资产源模板：
   /// 把 examples/ 目录当作资产根目录。
   struct Assets {
       base: PathBuf,
   }

   impl AssetSource for Assets {
       fn load(&self, path: &str) -> Result<Option<std::borrow::Cow<'static, [u8]>>> {
           fs::read(self.base.join(path))
               .map(|data| Some(std::borrow::Cow::Owned(data)))
               .map_err(|err| err.into())
       }

       fn list(&self, path: &str) -> Result<Vec<SharedString>> {
           Ok(Vec::new())
       }
   }

   struct MediaCard;

   impl Render for MediaCard {
       fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
           div()
               .flex()
               .flex_col()
               .items_center()
               .justify_center()
               .size_full()
               .bg(rgb(0xf5f5f5))
               .child(
                   div()                       // 卡片本体
                       .flex()
                       .flex_col()
                       .gap_4()
                       .p_6()
                       .w(px(420.))
                       .bg(gpui::white())
                       .border_1()
                       .border_color(rgb(0xdddddd))
                       .child(
                           div()               // 头部：SVG 图标 + 标题
                               .flex()
                               .flex_row()
                               .items_center()
                               .gap_3()
                               .child(
                                   svg()
                                       .path("image/color.svg")
                                       .size_8()
                                       .text_color(rgb(0x3b82f6)),
                               )
                               .child(
                                   StyledText::new("GPUI 内置元素")
                                       .with_highlights([
                                           (0..4, FontWeight::BOLD.into()),
                                           (5..11, rgb(0x3b82f6).into()),
                                       ]),
                               ),
                       )
                       .child(
                           img("image/app-icon.png")  // 内嵌资产图
                               .size(px(128.))
                               .object_fit(gpui::ObjectFit::Contain),
                       )
                       .child(
                           StyledText::new("text, img, svg, canvas")
                               .with_highlights([
                                   (0..4, rgb(0xcc0000).into()),
                                   (6..9, FontStyle::Italic.into()),
                                   (11..14, rgb(0x0000cc).into()),
                               ]),
                       ),
               )
       }
   }

   fn run_example() {
       application()
           .with_assets(Assets {
               base: PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("examples"),
           })
           .run(|cx: &mut App| {
               let bounds = Bounds::centered(None, size(px(500.0), px(500.0)), cx);
               cx.open_window(
                   WindowOptions {
                       window_bounds: Some(WindowBounds::Windowed(bounds)),
                       ..Default::default()
                   },
                   |_, cx| cx.new(|_| MediaCard),
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

   注意两处细节：高亮区间是 **UTF-8 字节偏移**且必须落在字符边界上——`"GPUI 内置元素"` 里 `0..4` 是 "GPUI"，`5..11` 是两个字各占 3 字节的「内置」（写成 `7..9` 这种切进字节的区间会在 debug 构建触发 [src/elements/text.rs:444-447](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/text.rs#L444-L447) 的断言 panic）；底部一行 `"text, img, svg, canvas"` 的三个区间分别命中 "text"、"img"、"svg"，区间之间的空隙回落到继承样式——这正是 4.1.2 节 `compute_runs` 的空隙补默认 run 规则。

2. 运行：`cargo run -p gpui --example media_card`。若提示找不到 example target，在 `Cargo.toml` 仿照 [Cargo.toml:177-179](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/Cargo.toml#L177-L179) 的 `[[example]]` 段补一条声明（`name = "media_card"`、`path = "examples/media_card.rs"`）。本仓库用显式 `[[example]]` 管理示例，补声明是最稳妥的做法。
3. 依次做三个小实验：
   - 删掉 svg 的 `.text_color(...)` → 图标消失；
   - 把 img 换成本地路径来源 `img(PathBuf::from("examples/image/app-icon.png"))`（需要 `use std::path::PathBuf;`，已有）→ 图片照常显示（`From<PathBuf> for ImageSource` 生效）；
   - 给 img 加 `.grayscale(true)` → 变灰度。

**需要观察的现象**：卡片垂直排列三个区块——头部是「蓝色 SVG 图标 + 加粗标题」，中部是 128px 的应用图标位图，底部一行三段不同颜色的文字；三个实验各自产生预期变化。

**预期结果**：SVG 缺色不绘制、路径来源与内嵌来源殊途同归、灰度开关即时生效。窗口尺寸、边框、间距全部由 u3-l2 的样式方法控制——内容元素只负责内容，布局仍归 div。若在无显示环境（headless CI）运行，图形窗口无法创建，此实践需在本地桌面环境验证。

## 6. 本讲小结

- GPUI 文本 API 是四层阶梯：字符串直接当 child（90% 场景）→ `text!` 宏（无障碍稳定 ID）→ `StyledText`（多 `TextRun` 混排，高亮延迟到布局期与继承样式合成）→ `InteractiveText`（区间可点击/悬停/tooltip）。
- `TextLayout` 用 `Rc<RefCell<Option<...>>>` 缓存测量结果，文本、换行宽度不变时跳过 shaping；`index_for_position`/`position_for_index` 是像素与字节下标互查的基础能力。
- `img()` 的关键是 `ImageSource` 四种来源（Resource 三形态 + Render + Image + Custom）；`&str` 按能否解析成 URL 区分 Uri 与 Embedded；加载态有 200ms 延迟防闪烁；动图由 `request_layout` 自己请求下一帧；`object_fit` 决定图片在元素框内的实际绘制区。
- `svg()` 有三种字节来源（`path`/`external_path`/`data`，内联数据用内容哈希当缓存键），颜色取自 `text_color`——不设色就不绘制；`Transformation` 只影响绘制不影响布局与命中盒。
- `canvas()` 是命令式绘制的逃生舱：两个 `FnOnce` 回调映射到 prepaint/paint，泛型 `T` 在两阶段间传状态；没有 ID 就没有跨帧元素状态，持久数据必须放在实体里。
- `SharedString` 回答「怎么存」（零拷贝句柄），`SharedUri` 回答「是什么」（URI 语义标记 + 缓存键）；选型时永远先问内置元素，再考虑 `canvas`，最后才是自定义 `Element`。

## 7. 下一步学习建议

- **下一讲 u3-l6（RenderOnce 与组件化复用）**：本讲的 `ImageContainer`（[examples/image/image.rs:43-69](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/examples/image/image.rs#L43-L69)）就是 `#[derive(IntoElement)]` + `RenderOnce` 的现成例子，把综合实践里的卡片抽象成组件是下一讲的天然练习。
- **第 4 单元 u4-l1（Element trait 三阶段生命周期）**：本讲反复出现的 `request_layout/prepaint/paint` 将被逐方法拆解，`Img` 的 `ImgState`（`with_element_state`）会解释「有 ID 的元素如何拥有跨帧状态」——正好对照 `Canvas` 没有 ID 的缺陷。
- **u6-l5（文本系统）**：`StyledText` 底层的 `shape_text`、`WrappedLine`、字形光栅化在那里展开；想理解 `with_runs` 的字节长度约束与换行宽度缓存，那是目的地。
- **u6-l7（图片、SVG 与资源缓存）**：`AssetSource`、`ImageCache`、`SvgRenderer` 与 resvg 集成的完整图景，解释本讲留下的「字符串路径如何异步变成像素」。
- 继续阅读建议：通读一遍 [src/elements/canvas.rs](https://github.com/zed-industries/zed/blob/2936989f1b7a15aaf7131b0a3c17961d706fdbf5/crates/gpui/src/elements/canvas.rs#L1-L96)（不到 100 行）作为第一个「完整 Element 实现」的样本，再带着问题进入第 4 单元。
