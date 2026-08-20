# 图片、SVG 与资源缓存

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `AssetSource`、`Resource`、`Asset` 三者的分工：资源**在哪里**、**怎么命名**、**怎么异步加载**。
2. 解释 `App::fetch_asset` 的「同键只加载一次」去重缓存，以及 `window.use_asset` 与 `get_asset` 的区别。
3. 掌握 `img()` 元素的完整生命周期：来源解析、异步加载、尺寸推导（aspect_ratio / Auto 宽高）、200ms 加载延迟替身、`ObjectFit` 对位与最终进入精灵图集的绘制。
4. 理解为什么需要独立的 `ImageCache` 体系（`RetainAllImageCache`、`image_cache()` 元素、`div().image_cache()`），以及如何实现自定义淘汰策略（LRU）。
5. 理解 `SvgRenderer` 与 resvg/usvg 的集成：解析、按尺寸光栅化、2 倍平滑系数、8192 上限，以及 `svg()` 元素「单色蒙版 + 文本着色」的绘制路径。
6. 会排查「SVG/图片不显示」的常见原因。

本讲是 u3-l5（内置元素一览）的深化：那一讲我们知道了 `img`/`svg` 怎么用，这一讲我们追问字节从哪里来、解码结果存在哪里、什么时候会被回收。

## 2. 前置知识

- **元素三阶段**（u4-l1）：`request_layout` → `prepaint` → `paint`。`img()` 的加载、尺寸推导、动画推进都发生在 `request_layout`；真正的纹理提交发生在 `paint`。
- **实体与上下文**（u2）：`Entity<T>`、`cx.new`、`cx.notify`。图片缓存本身就是一个实体。
- **后台执行器**（u2-l5）：`cx.background_executor().spawn(fut)` 把满足 `Send` 的 future 丢到平台线程池；`Task` 可被 `.shared()` 成多个共享句柄。
- **精灵图集（sprite atlas）**：GPU 纹理按瓦片（tile）分配复用。图片和 SVG 光栅化结果都以瓦片形式进入图集，键分别是 `RenderImageParams`（image_id + frame_index）与 `RenderSvgParams`（path + size）。
- **EXIF 方向**：JPEG 等格式可能在元数据里记录「相机旋转了 90°」，解码时需要应用该旋转才是正确朝向。
- **RGBA / BGRA**：`image` crate 解码出 RGBA 字节序，而 GPU 采样通常期望 BGRA，所以解码后要做一次逐像素交换。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/assets.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/assets.rs) | `AssetSource` trait（应用资源从哪来）、`ImageId`、解码后的 `RenderImage`（BGRA 帧容器） |
| [src/asset_cache.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/asset_cache.rs) | `Resource`（三种资源地址）、`Asset` trait（异步资产协议）、`AssetLogger`（错误日志包装）、`hash` |
| [src/app.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs) | `with_assets` 装配资源源；`loading_assets` 表与 `fetch_asset`/`remove_asset`/`has_asset` 去重缓存 |
| [src/elements/img.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs) | `img()` 元素：`ImageSource`、`StyledImage`（grayscale/object_fit/loading/fallback）、`ImageAssetLoader`（真正的解码逻辑）、`ImageCacheError` |
| [src/elements/image_cache.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs) | `ImageCache` trait、`image_cache()` 元素、`RetainAllImageCache`、`retain_all(id)` 便捷 provider |
| [src/elements/svg.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/svg.rs) | `svg()` 元素：path/external_path/data 三种来源、单色绘制路径 |
| [src/svg_renderer.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs) | `SvgRenderer`：usvg 解析（含字体解析器）、resvg 光栅化、2 倍平滑、8192 尺寸上限 |
| [src/platform.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs) | `Image`（格式 + 原始字节）与 `to_image_data`（按格式分发解码，SVG 走 SvgRenderer）；`decode_static_image` |
| [src/style.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/style.rs) | `ObjectFit` 枚举与 `get_bounds` 几何计算 |
| [examples/image_loading.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image_loading.rs) | 演示加载中/失败替身、`LOADING_DELAY`、`remove_asset` 强制重载 |
| [examples/image/image.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image/image.rs) | 本地文件 + 远程 URL + AssetSource 内嵌资源三路对比 |
| [examples/image_gallery.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image_gallery.rs) | 手动 `RetainAllImageCache` 与自定义 `SimpleLruCache` 两种缓存策略对照 |
| [examples/svg/svg.rs](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/svg/svg.rs) | 同一 SVG 多尺寸、多颜色渲染 |

## 4. 核心概念与源码讲解

### 4.1 AssetSource 与 Resource：资源从哪里来

#### 4.1.1 概念说明

GPUI 把「应用资源」（图标、字体、打包进二进制的图片等）抽象为一个极小的接口 `AssetSource`：给定一个字符串路径，返回字节。它回答的是**资源在哪里**的问题——可能打包在二进制里、可能在文件系统某目录下、也可能由嵌入宿主（如 web）提供。

而**资源怎么命名**由 `Resource` 枚举统一：URI（远程）、文件系统路径、内嵌路径（交给 AssetSource 解析）。`ImageSource` 从字符串转换时用 `url::Url` 能否解析来区分前两者。

解码后的统一容器是 `RenderImage`：一串 BGRA 格式的帧（动图多帧、静图一帧）加一个全局唯一 `ImageId`。`ImageId` 之后就是精灵图集的缓存键。

#### 4.1.2 核心流程

```text
字符串 "https://..."  ──is_uri?──>  Resource::Uri       （HTTP GET）
字符串 "svg/dragon.svg" ─────────>  Resource::Embedded  （AssetSource::load 查询）
PathBuf / Arc<Path>    ─────────>  Resource::Path       （fs::read）

Resource ──异步加载+解码──> RenderImage { id: ImageId, frames: [BGRA...], scale_factor }
```

#### 4.1.3 源码精读

`AssetSource` 只有两个方法，`load` 返回 `Option` 表示「该路径下没有这个资产」：

- [src/assets.rs:L12-L19](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/assets.rs#L12-L19) — 定义 `AssetSource` trait：`load(path)` 取字节、`list(path)` 列目录。
- [src/assets.rs:L21-L29](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/assets.rs#L21-L29) — `()` 也实现了 `AssetSource`，永远返回 `None`/空。这是**默认实现**：如果应用没调 `with_assets`，所有内嵌资源查询都会落空——这正是「SVG 配了 path 却不显示」的第一嫌疑。
- [src/asset_cache.rs:L10-L19](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/asset_cache.rs#L10-L19) — `Resource` 三形态：`Uri(SharedUri)` / `Path(Arc<Path>)` / `Embedded(SharedString)`，派生 `Hash` 作为缓存键的基础。
- [src/app.rs:L200-L208](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L200-L208) — `Application::with_assets` 把 `Arc<dyn AssetSource>` 存入 `App`，并**同时用它构造 `SvgRenderer`**。也就是说 AssetSource 同时服务位图和 SVG 两条链路。
- [src/elements/img.rs:L63-L71](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L63-L71) — `From<&str> for ImageSource`：用 `url::Url::from_str` 判断是否 URI，是则 `Resource::Uri`，否则 `Resource::Embedded`。注意这**不区分文件系统路径**——裸字符串永远不走 `Resource::Path`。
- [src/assets.rs:L42-L49](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/assets.rs#L42-L49) — `RenderImage` 结构：`id`、`scale_factor`（crate 私有）、`SmallVec<[Frame; 1]>`（单帧内联、动图堆分配）。
- [src/assets.rs:L59-L69](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/assets.rs#L59-L69) — `RenderImage::new` 用静态 `AtomicUsize` 发号，每个实例拿到全局唯一 `ImageId`。
- [src/assets.rs:L89-L93](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/assets.rs#L89-L93) — `render_size`：显示尺寸 = 物理像素 ÷ `scale_factor`。SVG 以 2 值渲染时（见 4.5）`scale_factor` 为 2，逻辑显示尺寸折半，保证布局不因超采样而变大。
- [src/assets.rs:L96-L106](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/assets.rs#L96-L106) — `delay(frame_index)` 返回动图该帧相对上一帧的延时；`frame_count` 返回帧数。GIF 推进动画就靠这两个值（见 4.3）。

示例侧的 AssetSource 一般就是把路径拼到某个基目录再 `fs::read`：

- [examples/image/image.rs:L16-L41](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image/image.rs#L16-L41) — 示例 `Assets`：`base.join(path)` 后读文件，`list` 用 `read_dir`。真实工程（如 Zed 主程序）会换成从打包资源读取的实现。

#### 4.1.4 代码实践

1. **实践目标**：确认 AssetSource 的接线方式，并体会「没配置就查不到」。
2. **操作步骤**：
   - 运行 `cargo run -p gpui --example svg`（在 `crates/gpui` 目录下），窗口应显示三条不同颜色的龙形 SVG——它们的字节来自 `Assets { base: examples 目录 }`。
   - 把 [examples/svg/svg.rs:L75-L77](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/svg/svg.rs#L75-L77) 中 `with_assets(...)` 改成 `application().run(...)`（去掉资产源，等于用 `()` 默认实现），再运行。
   - 恢复代码。
3. **需要观察的现象**：去掉 `with_assets` 后窗口空白（`svg()` 拿不到字节、拿不到 `text_color` 时都不绘制）；恢复后图标重现。
4. **预期结果**：`Resource::Embedded("svg/dragon.svg")` 的解析完全依赖 AssetSource；没有资产源时静默失败（日志里可能有 warn，取决于实现）。
5. 此实践需要本地能编译运行 GPUI 示例，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `img("image/app-icon.png")` 与 `img(Path::new("image/app-icon.png"))` 走的是完全不同的加载路径？
**答案**：前者经 `From<&str>` 判定不是 URI，得到 `Resource::Embedded`，由 `AssetSource::load` 解析（相对 AssetSource 的 base）；后者经 `From<&Path>` 得到 `Resource::Path`，由 `ImageAssetLoader` 直接 `fs::read`（相对进程工作目录）。参考 [src/elements/img.rs:L63-L99](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L63-L99)。

**练习 2**：两个内容完全相同的 PNG 分别用 `Resource::Uri` 加载两次，会得到同一个 `ImageId` 吗？
**答案**：不会。`ImageId` 由 `RenderImage::new` 里的全局计数器单调发号（[src/assets.rs:L62-L68](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/assets.rs#L62-L68)），与内容无关；去重发生在更上游的 `(TypeId, hash(source))` 键上（见 4.2）。

**练习 3**：`RenderImage::render_size` 为什么要除以 `scale_factor`？
**答案**：为了超采样渲染（如 SVG 以 2 倍尺寸光栅化）不改变逻辑布局尺寸：物理像素翻倍、`scale_factor` 记为 2，显示尺寸 = 物理 ÷ 2，与逻辑坐标一致。

### 4.2 Asset 协议与 fetch_asset：全局去重缓存

#### 4.2.1 概念说明

`Asset` 是 GPUI 的**通用异步资产协议**：任何「给定一个可哈希的 Source，异步产出一个可克隆 Output」的东西都可以实现它。图片、SVG 字节、字体、甚至示例里「带超时参数的图片加载」都是 Asset。它解决两个问题：

1. **异步**：加载在后台执行器跑，不阻塞 UI 线程。
2. **去重**：同一时间对同一 `(Asset 类型, Source)` 的多次请求只触发一次 `load`，后来者共享同一个 `Shared<Task>`。

这套缓存在 `App::loading_assets` 表里，键是 `(TypeId, u64)`——**永不淘汰**，这正是后面需要独立 `ImageCache` 的原因。

#### 4.2.2 核心流程

```text
window.use_asset::<A>(&source, cx)
  └─ cx.fetch_asset::<A>(&source)
       键 k = (TypeId::of::<A>(), hash(source))
       若 k 已在 loading_assets：取出共享 task，is_first = false
       否则：A::load(source, cx) → background_executor().spawn(...).shared() 存入表
  └─ task.now_or_never()
       已完成 → 直接返回 Some(output)   （缓存命中）
       未完成   → 若 is_first，spawn 一个等待 task 完成后
                  on_nextFrame 通知当前视图重绘 → 返回 None
```

下一帧视图重新 render，再次走到 `use_asset`，此时任务已完成，拿到数据。

#### 4.2.3 源码精读

- [src/asset_cache.rs:L39-L52](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/asset_cache.rs#L39-L52) — `Asset` trait：`Source: Clone + Hash + Send` 是缓存键；`Output: Clone + Send` 是结果；`load` 返回 `Send + 'static` future（会被丢到后台线程池）。
- [src/asset_cache.rs:L60-L77](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/asset_cache.rs#L60-L77) — `AssetLogger<T>`：包装另一个 Asset，`inspect_err` 把 `Err` 打进日志。用于「失败别刷屏也别无声」——`img()` 的资源加载都包了这一层。
- [src/asset_cache.rs:L79-L82](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/asset_cache.rs#L79-L82) — `hash`：FxBuildHasher 的非加密哈希，够快够散列即可。
- [src/app.rs:L732](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L732) — `loading_assets: FxHashMap<(TypeId, u64), Box<dyn Any>>`：类型擦除的全局资产表。
- [src/app.rs:L2639-L2656](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L2639-L2656) — `fetch_asset` 的全部逻辑：先 `remove` 再 `unwrap_or_else` 创建再 `insert` 回去（绕过 borrow 检查的惯用法），返回 `(Shared<Task>, is_first)`。
- [src/window.rs:L3667-L3692](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L3667-L3692) — `Window::use_asset`：`now_or_never()` 试探是否已完成；未完成且是首个请求者时，spawn 等待任务并在**下一帧** `cx.notify(entity_id)` 通知**当前正在绘制的视图**重绘。
- [src/window.rs:L3694-L3702](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L3694-L3702) — `get_asset`：同样去重加载，但**不会**在完成后触发重绘。适合「顺手预热」或非渲染用途。
- [src/app.rs:L2621-L2633](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L2621-L2633) — `remove_asset` 主动逐出缓存（下次 `use_asset` 会重新加载）；`has_asset` 仅测试构建可用，用来断言缓存命中。

#### 4.2.4 代码实践

1. **实践目标**：用 `remove_asset` 反证缓存的存在——逐出后才会重新加载。
2. **操作步骤**：运行 `cargo run -p gpui --example image_loading`。窗口里有四张方图：第一张很快加载完成（不闪加载态），第二张延迟 5 秒（先出现呼吸式加载动画），第三张 5 秒后失败（显示 `?` 占位），第四张指向不存在的路径（直接失败）。**点击任一张图片**，观察它经历一次完整的「加载中 → 显示」循环。
3. **需要观察的现象**：点击后图片消失并重新走加载流程。点击处理在 [examples/image_loading.rs:L130-L132](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image_loading.rs#L130-L132)：`cx.remove_asset::<LoadImageWithParameters>(&image_source)`。若没有缓存，逐出就没有任何可观察效果。
4. **预期结果**：不点击时图片常驻（缓存命中，无重复加载）；点击后强制重载，证明「命中缓存」与「逐出重载」是同一个键空间的正反两面。
5. 此示例还需要 `examples/image/app-icon.png` 存在（已确认存在），运行表现**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`use_asset` 与 `get_asset` 的核心差异是什么？各自适合什么场景？
**答案**：`use_asset` 在任务完成后的下一帧通知当前视图重绘（渲染驱动的加载）；`get_asset` 不触发重绘（预热、后台统计）。参考 [src/window.rs:L3667-L3702](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L3667-L3702)。

**练习 2**：为什么 `fetch_asset` 里要先 `remove` 再 `insert`？
**答案**：为了在拿到表内任务或创建新任务的同一处代码路径里，避免对 `HashMap` 的双重可变借用；`remove` 拿走所有权、判空、必要时新建，最后统一 `insert` 回去。见 [src/app.rs:L2642-L2653](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L2642-L2653)。

**练习 3**：`loading_assets` 这张表什么时候释放条目？
**答案**：只有显式调用 `remove_asset`（[src/app.rs:L2621-L2625](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/app.rs#L2621-L2625)）。没有 LRU、没有容量上限——这就是 4.4 要引入 `ImageCache` 的动机。

### 4.3 Img 元素：从 ImageSource 到屏幕上的纹理

#### 4.3.1 概念说明

`img()` 是位图（与 SVG-as-位图）的声明式入口。它的三阶段里，`request_layout` 承担了远超「申报尺寸」的职责：

- 触发异步加载（经缓存解析 `ImageSource`）；
- 用图片内在尺寸推导 `aspect_ratio` 和 Auto 宽高；
- 推进动图帧（GIF/WebP）并申请下一动画帧；
- 决定是否渲染 loading / fallback 替身元素。

`paint` 则按 `ObjectFit` 计算图像实际落位，把帧提交进精灵图集。

#### 4.3.2 核心流程

```text
request_layout:
  source.use_data(cache, window, cx)
    ├─ Some(Ok(data))  → 推导 aspect_ratio；Auto 宽/高按内在比例补全；
    │                    多帧且窗口活跃且未 reduce_motion → 按帧延时推进 frame_index
    │                    并 window.request_animation_frame()
    ├─ Some(Err(_))    → 渲染 fallback 替身（若配置）
    └─ None（加载中）  → 记录 started_loading 时刻；超过 LOADING_DELAY(200ms)
                         渲染 loading 替身；首次还会 spawn 一个 200ms 定时器
                         到点通知视图重绘
paint:
  再次 use_data → 拿到数据则 object_fit.get_bounds(bounds, 图片尺寸)
  → window.paint_image(...)（按 RenderImageParams 查/建图集瓦片）
```

尺寸推导的关键不变式（设内在宽高 \(w_i, h_i\)）：若用户只给了高度 \(h\)，宽度取 \(w = w_i \cdot h / h_i\)；只给宽度同理；都不给则用内在尺寸。若用户显式设置了 `aspect_ratio`，则**不被内在比例覆盖**。

#### 4.3.3 源码精读

- [src/elements/img.rs:L40-L51](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L40-L51) — `ImageSource` 四形态：`Resource`（走缓存/资产系统）、`Render(Arc<RenderImage>)`（已是解码结果，零加载）、`Image(Arc<Image>)`（原始字节，经 `ImageDecoder` 资产解码）、`Custom(闭包)`（完全自定义加载函数）。
- [src/elements/img.rs:L31-L38](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L31-L38) — `LOADING_DELAY = 200ms`：加载快于 200ms 的图**不闪**加载态；`ImgResourceLoader` 就是 `AssetLogger<ImageAssetLoader>` 的别名。
- [src/elements/img.rs:L128-L177](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L128-L177) — `ImageStyle` 与 `StyledImage` trait：`grayscale`、`object_fit`（默认 `Contain`）、`with_fallback`（失败替身）、`with_loading`（加载替身）。
- [src/elements/img.rs:L211-L218](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L211-L218) — `Img::extensions()`：`image` crate 支持的全部格式扩展名加 `svg`——问答「img 能显示什么」的权威清单。
- [src/elements/img.rs:L308-L314](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L308-L314) — 加载入口在 `request_layout` 内：缓存解析顺序是「元素显式 `.image_cache()` → 窗口 `image_cache_stack` 栈顶（最近祖先）」。
- [src/elements/img.rs:L348-L380](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L348-L380) — 尺寸推导：`aspect_ratio` 未设则用内在宽高比；`Length::Auto` 的宽（或高）按另一维的显式绝对长度等比换算，否则直接取内在尺寸。
- [src/elements/img.rs:L319-L346](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L319-L346) — 动图推进：`elapsed >= frame_duration` 时 `frame_index` 前进并结算剩余时间（`current_time - (elapsed - frame_duration)`），窗口不活跃或 `reduce_motion` 时暂停并清空计时。
- [src/elements/img.rs:L382-L388](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L382-L388) — 多帧动图持续 `request_animation_frame()`，这就是 u3-l5 说「动图帧由布局阶段自驱」的出处。
- [src/elements/img.rs:L400-L423](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L400-L423) — 加载中分支：首帧记录 `(Instant, Task)`，Task 是 200ms 定时器到点 `cx.notify` 当前视图；之后每帧检查 `elapsed > LOADING_DELAY` 才挂 loading 替身。
- [src/elements/img.rs:L461-L510](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L461-L510) — `paint`：`object_fit.get_bounds(...)` 算出图像实际边界，`window.paint_image` 提交；失败/加载中则改画替身元素。
- [src/style.rs:L28-L40](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/style.rs#L28-L40) — `ObjectFit` 五档：`Fill`（拉伸）、`Contain`（完整放入留白）、`Cover`（填满裁切）、`ScaleDown`（只在更大时缩小）、`None`（原始尺寸）。几何计算在 [src/style.rs:L42-L48](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/style.rs#L42-L48) 的 `get_bounds`，思路是比较容器与图像的宽高比。
- [src/window.rs:L4493-L4529](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4493-L4529) — `paint_image`：先剔除零尺寸相交，再以 `RenderImageParams { image_id, frame_index }` 为键 `sprite_atlas.get_or_insert_with`——**同一张图同一帧永远只上传一次纹理**，之后帧帧复用。
- [src/elements/img.rs:L619-L667](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L619-L667) — `ImageAssetLoader::load` 的取字节三分支：`Path` → `fs::read`；`Uri` → HTTP GET，非成功状态返回带首行 body 的 `BadStatus`；`Embedded` → `asset_source.load`，找不到报 `ImageCacheError::Asset`。
- [src/elements/img.rs:L669-L738](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L669-L738) — 解码分发：`image::guess_format` 识别格式；GIF/WebP 且动图则逐帧解码并 RGBA→BGRA 交换（坏帧跳过、全坏报错）；其余走 `decode_static_image`。
- [src/elements/img.rs:L739-L743](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L739-L743) — 猜不出位图格式时**当 SVG 处理**：`svg_renderer.render_single_frame(&bytes, 1.0)`。即 `img()` 也能显示 SVG，但按 scale 1.0 光栅化一次，之后靠纹理缩放。
- [src/platform.rs:L2567-L2592](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L2567-L2592) — `decode_static_image`：应用 EXIF 方向后转 RGBA8 再交换为 BGRA——`image/image.rs` 里那张「EXIF orientation」图片验证的就是这条路径。
- [src/elements/img.rs:L749-L776](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L749-L776) — `ImageCacheError` 枚举：`Other`/`Io`/`BadStatus`/`Asset`/`Image`/`Usvg`，排错时按变体对号入座。

#### 4.3.4 代码实践

1. **实践目标**：观察 `ObjectFit` 与加载替身的实际行为。
2. **操作步骤**：
   - 运行 `cargo run -p gpui --example image`（需要网络，示例已装配 ReqwestClient，见 [examples/image/image.rs:L162-L166](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image/image.rs#L162-L166)）。
   - 在 [examples/image/image.rs:L92-L94](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image/image.rs#L92-L94) 的远程图上依次加 `.object_fit(ObjectFit::Cover)`、`.object_fit(ObjectFit::Fill)`、`.grayscale(true)`、`.size(px(256.))`，每次改完重跑对比。
   - 再给远程图加 `.with_loading(|| div().bg(gpui::gray()).size_full().into_any_element())`，断网或用慢速 URL（如 `https://picsum.photos/800/400?sleep` 不生效时可换大图）观察 200ms 后出现灰块。
3. **需要观察的现象**：`Cover` 裁切填满、`Fill` 拉伸变形、`Contain` 留白；灰度开关即时生效；慢加载时替身元素顶替位置、图片就绪后无缝切换。
4. **预期结果**：替身元素参与布局（替身有自己的 `request_layout`，见 [src/elements/img.rs:L391-L409](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L391-L409)），切换不跳动。
5. 网络相关表现**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一张 800×400 的图放在 `img(...).h(px(180.))` 的容器里，最终布局宽度是多少？
**答案**：高度显式 180px、宽度 `Auto`，按内在比例推导为 \(800 \times 180 / 400 = 360\) 逻辑像素。这正是 [examples/image/image.rs:L121-L131](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image/image.rs#L121-L131) 「Auto Width / Auto Height」两栏演示的行为，推导逻辑在 [src/elements/img.rs:L354-L380](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L354-L380)。

**练习 2**：用户同时设置了 `.aspect_square()` 和一张竖版图，谁的宽高比生效？
**答案**：用户的。`request_layout` 只在 `style.aspect_ratio.is_none()` 时才写入内在比例（[src/elements/img.rs:L350-L352](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L350-L352)）。对应测试 `explicit_aspect_ratio_is_not_overridden_by_intrinsic_ratio`（[src/elements/img.rs:L901-L937](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L901-L937)）。

**练习 3**：为什么 `img()` 在 `request_layout` 就要调用 `use_data`，而不是等到 `paint`？
**答案**：因为布局需要图片的内在尺寸来推导 `aspect_ratio` 与 Auto 边；此外加载完成的重绘通知也依赖布局期注册的视图。`paint` 里会再次调用 `use_data` 拿最新数据（[src/elements/img.rs:L480-L486](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L480-L486)）。

### 4.4 ImageCache：可淘汰的图片缓存体系

#### 4.4.1 概念说明

`App::loading_assets` 全局表永不淘汰，对「无限滚动的图墙」意味着 GPU 纹理只增不减。`ImageCache` 体系把**淘汰权**交还给应用：

- `ImageCache` trait：`load(resource) -> Option<Result<RenderImage>>`，`None` 表示还在加载——与 `Asset` 协议的返回约定一致。
- `image_cache(provider)` 元素 / `div().image_cache(provider)`：为子树压入一个缓存栈帧。
- `RetainAllImageCache`：官方提供的「全保留」实现（去重但不淘汰），实体释放时统一 `drop_image`。
- `retain_all(id)`：以元素状态惰性创建缓存的便捷 provider。
- 自定义实现（如示例的 `SimpleLruCache`）：按 LRU 淘汰并在逐出时 `drop_image` 归还图集瓦片。

#### 4.4.2 核心流程

```text
img 元素解析缓存：显式 .image_cache() → window.image_cache_stack 栈顶 → 全局 use_asset
                         ▲
 image_cache(provider) 元素在 request_layout / paint 阶段压栈 ──┘

RetainAllImageCache::load(resource):
  hash = hash(resource)
  命中 → item.get()（Loading 态用 now_or_never 晋升为 Loaded）
  未命中 → AssetLogger::<ImageAssetLoader>::load → background_spawn → .shared()
           存 ImageCacheItem::Loading
           spawn 窗口任务：await 完成后 on_next_frame 通知当前视图
  实体释放（observe_release）→ 对所有已加载图片 cx.drop_image（释放图集瓦片）
```

`ImageCacheItem` 是「任务或结果」的单格容器：`Loading(Shared<Task>)` 首次 `get()` 成功后原地改写为 `Loaded(Result)`，之后命中直接克隆。

#### 4.4.3 源码精读

- [src/elements/image_cache.rs:L201-L212](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L201-L212) — `ImageCache` trait 契约：`None` = 还在加载。文档明确要求实现者保证「不再需要的图片从所有窗口移除」。
- [src/elements/image_cache.rs:L23-L55](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L23-L55) — `AnyImageCache`：类型擦除的缓存句柄（`AnyEntity` + 函数指针），让 `img()` 不必知道缓存的具体类型。
- [src/elements/image_cache.rs:L110-L129](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L110-L129) 与 [L145-L161](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L145-L161) — `ImageCacheElement` 在 `request_layout` 和 `paint` 两个阶段用 `window.with_image_cache` 压栈后遍历孩子；注意 **prepaint 不压栈**，因为 `img` 不在 prepaint 取数据。
- [src/window.rs:L4804-L4817](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4804-L4817) — `with_image_cache`：压/弹 `image_cache_stack`（字段定义在 [src/window.rs:L1159](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L1159)），子树内 `img` 取「栈顶」即最近祖先的缓存。
- [src/elements/div.rs:L1719-L1723](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/div.rs#L1719-L1723) — `Div::image_cache`：不必引入专门元素，任何 div 都能就地挂缓存。
- [src/elements/image_cache.rs:L167-L199](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L167-L199) — `ImageCacheItem` 与其 `get()`：`Loading` 态用 `now_or_never()` 非阻塞试探，完成即原地晋升 `Loaded`。
- [src/elements/image_cache.rs:L240-L252](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L240-L252) — `RetainAllImageCache::new`：创建实体的同时注册 `observe_release`——实体被释放时把所有已加载图片 `drop_image`，防止纹理泄漏。
- [src/elements/image_cache.rs:L254-L286](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L254-L286) — 核心 `load`：命中走 `item.get()`；未命中启动共享任务并 spawn 窗口任务「await 完成后下一帧 `cx.notify(entity)`」。与 `window.use_asset` 的重绘策略同构，但缓存实体由应用持有。
- [src/elements/image_cache.rs:L288-L305](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L288-L305) — `clear` / `remove`：批量或单个逐出并 `drop_image`。
- [src/elements/image_cache.rs:L329-L353](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L329-L353) — `retain_all(id)`：用 `with_global_id` + `with_element_state` 把 `Entity<RetainAllImageCache>` 存进元素状态，首次渲染时惰性创建、跨帧复用。
- [examples/image_gallery.rs:L166-L246](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image_gallery.rs#L166-L246) — `SimpleLruCache`：`usages` Vec 记录访问序（最近访问插到下标 0），容量满时弹出最旧、`drop_image` 归还瓦片——自定义淘汰策略的完整范本。

#### 4.4.4 代码实践

1. **实践目标**：对照「手动管理缓存」与「自动 LRU 缓存」两种模式的内存行为。
2. **操作步骤**：运行 `cargo run -p gpui --example image_gallery`（需网络）。窗口上半区是手动 `RetainAllImageCache`（视图持有实体，点 Next Photos 时显式 `clear`），下半区是 `SimpleLruCache`（容量 30，自动淘汰）。反复点 **Next Photos** 按钮切换批次。
3. **需要观察的现象**：计数器只增不减；两区图片都会加载显示。用系统监视器观察进程内存：上半区若不点按钮会持续增长，下半区受 LRU 约束更平稳。
4. **预期结果**：理解 [examples/image_gallery.rs:L24-L37](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image_gallery.rs#L24-L37) 里 `clear` + 换 URL 的组合拳——旧 URL 的缓存条目被显式清空，新 URL 重新加载。
5. 内存曲线**待本地验证**（取决于平台图集实现）。

#### 4.4.5 小练习与答案

**练习 1**：`img()` 元素的缓存解析顺序是什么？
**答案**：① 元素上显式 `.image_cache(&entity)`（[src/elements/img.rs:L220-L235](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L220-L235)）；② 窗口 `image_cache_stack` 栈顶（最近的 `image_cache()` 元素或 `div().image_cache()` 祖先）；③ 都没有则退回全局 `window.use_asset::<ImgResourceLoader>`。见 [src/elements/img.rs:L536-L549](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/img.rs#L536-L549)。

**练习 2**：`RetainAllImageCache` 与全局 `loading_assets` 都「不淘汰」，二者本质区别在哪？
**答案**：生命周期归属。`RetainAllImageCache` 是应用持有的实体，释放时经 `observe_release` 自动 `drop_image` 归还 GPU 瓦片（[src/elements/image_cache.rs:L243-L250](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L243-L250)）；`loading_assets` 挂在 App 上，只能显式 `remove_asset` 逐出。

**练习 3**：为什么 `ImageCacheElement` 的 `prepaint` 不需要压缓存栈？
**答案**：`img()` 只在 `request_layout`（取尺寸/推进动画）和 `paint`（取帧数据）两个阶段访问缓存，prepaint 阶段子树没有缓存消费者；压栈只在确有消费的两个阶段发生（对照 [src/elements/image_cache.rs:L131-L161](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L131-L161)）。

### 4.5 SvgRenderer 与 resvg：SVG 的解析与光栅化

#### 4.5.1 概念说明

SVG 是矢量格式，「显示尺寸」和「光栅化尺寸」可以分离。GPUI 的 `SvgRenderer` 封装了这条管线：

- **usvg** 负责解析：把 SVG 文本变成可渲染树，同时解析字体（SVG 里的 `<text>` 要选字体）、把文本转为路径。
- **resvg + tiny-skia** 负责光栅化：按目标尺寸把树画进像素图（Pixmap）。

两条消费路径：

1. `svg()` 元素（单色图标）：`window.paint_svg` → `render_alpha_mask` 只取 **alpha 蒙版**，进图集后用 `text_color` 着色成 `MonochromeSprite`。同一 path + 同一尺寸命中同一图集瓦片；换颜色**不**重新光栅化。
2. `img()` 加载 SVG：`ImageAssetLoader` → `render_single_frame(bytes, 1.0)` 光栅化成一张普通 `RenderImage`，走位图管线（可灰度、可 object_fit，但缩放即纹理缩放）。

#### 4.5.2 核心流程

```text
svg() 元素 paint:
  RenderSvgParams { path, size = bounds × SMOOTH_SVG_SCALE_FACTOR(2) }
  sprite_atlas.get_or_insert_with(params)   ← 缓存键：path + 尺寸
    未命中 → svg_renderer.render_alpha_mask(params, bytes)
             → usvg 解析 → resvg 光栅化 → 取每像素 alpha
  MonochromeSprite { color = text_color, tile } 插入场景

img() 加载 SVG:
  ImageAssetLoader → guess_format 失败 → render_single_frame(bytes, 1.0)
  → parse_svg + render_parsed(ScaleFactor(1.0 × 2), scale_factor=2)
  → RenderImage（render_size 折半回到逻辑尺寸）
```

超采样收益：以 2 倍尺寸光栅化、按 1 倍尺寸显示，边缘更平滑。代价是 4 倍像素量，因此有 8192px 的安全上限。

#### 4.5.3 源码精读

- [src/svg_renderer.rs:L80-L81](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs#L80-L81) — `SMOOTH_SVG_SCALE_FACTOR = 2.0`：所有 SVG 光栅化的固定超采样系数。
- [src/svg_renderer.rs:L122-L187](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs#L122-L187) — `SvgRenderer::new`：用 `AssetSource` 构造，装配自定义 usvg 字体解析器——系统字体库惰性克隆 + 从资产源加载内置字体（`fonts/ibm-plex-sans/...`、`fonts/lilex/...`）+ 修正 CSS 通用字体族 + emoji 回退选择。**SVG 里的文字能正常显示全靠这里**。
- [src/svg_renderer.rs:L97-L114](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs#L97-L114) — `ParsedSvg` 与 `SvgSize`：解析结果可复用于多个尺寸；`SvgSize` 三形态——`Size`（定宽保比）、`ExactSize`（精确宽高）、`ScaleFactor`（乘以 SVG 自带尺寸）。
- [src/svg_renderer.rs:L189-L193](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs#L189-L193) — `parse_svg`：一行 `usvg::Tree::from_data`。文档注释点明：解析会解析字体并转路径，**同一 SVG 多尺寸渲染应复用 `ParsedSvg`**。
- [src/svg_renderer.rs:L195-L221](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs#L195-L221) — `render_parsed`：`ScaleFactor` 分支先乘 2 再记 `image_scale_factor = 2`；光栅化后 `swap_rgba_pa_to_bgra` 转字节序，产出 `RenderImage` 并写入 `scale_factor`。
- [src/svg_renderer.rs:L272-L309](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs#L272-L309) — `rasterize_tree`：按 `SvgSize` 换算目标宽高；超过 `MAX_SIZE = 8192` 会 warn 并整体缩回（避免纹理分配 panic，issue #56466）；最后 `resvg::render` 进 Pixmap。
- [src/svg_renderer.rs:L223-L231](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs#L223-L231) — `render_single_frame`：parse + render 的便捷组合，`img()` 路径与 `Image::to_image_data` 的 SVG 分支都用它。
- [src/svg_renderer.rs:L233-L264](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs#L233-L264) — `render_alpha_mask`：为 `svg()` 元素服务，把 Pixmap 每像素压成单字节 alpha；字节可由调用方直给，否则用 `asset_source.load(&params.path)` 取——`svg().path(...)` 的字节来源就在这里接上 AssetSource。
- [src/window.rs:L4423-L4455](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4423-L4455) — `Window::paint_svg`：`RenderSvgParams.size` 由 bounds 乘平滑系数向上取整而来；`sprite_atlas.get_or_insert_with` 以 (path, size) 为键缓存蒙版瓦片——**同尺寸复用、换尺寸重光栅化**。
- [src/window.rs:L4456-L4480](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4456-L4480) — 蒙版瓦片尺寸除回 2 得到显示边界，插入 `MonochromeSprite` 并以 `color.opacity(element_opacity)` 着色。
- [src/elements/svg.rs:L38-L69](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/svg.rs#L38-L69) — `Svg` 的三种来源：`path`（AssetSource 解析）、`external_path`（直接 `fs::read`）、`data(&[u8])`（对字节做哈希生成 `__binary_svg__{hash}` 形式的缓存路径）。
- [src/elements/svg.rs:L149-L186](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/svg.rs#L149-L186) — `paint` 三分支；注意每个分支都与 `style.text.color` 配对（`zip`）——**没设文字颜色就不画**，这是「SVG 不显示」的第二嫌疑。
- [src/platform.rs:L2688-L2692](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/platform.rs#L2688-L2692) — `Image::to_image_data` 的 SVG 分支：持有原始字节的 `Image`（如来自剪贴板）也经 `render_single_frame` 变可渲染。

#### 4.5.4 代码实践

1. **实践目标**：验证「同一 SVG、多个尺寸」的缓存行为与清晰度差异。
2. **操作步骤**：
   - 运行 `cargo run -p gpui --example svg`：三条同尺寸不同颜色的龙。把 [examples/svg/svg.rs:L52-L70](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/svg/svg.rs#L52-L70) 中三个 `.size_8()` 分别改成 `.size_8()`、`.size_16()`、`.size_32()` 再运行。
   - 对比实验：在同一个示例里加一个 `img("svg/dragon.svg").size_16()`（img 走位图管线）与 `svg().path("svg/dragon.svg").size_16().text_color(rgb(0x000000))` 并排放大观察边缘。
3. **需要观察的现象**：三种颜色共享同一蒙版瓦片（颜色只是着色，不触发重新光栅化）；三个尺寸各占一个瓦片，放大窗口时边缘依然锐利；`img` 版 SVG 是一次光栅化后纹理缩放，放大可能发糊。
4. **预期结果**：理解缓存键差异——`svg()` 键为 (path, size)，`img()` 键为 (image_id, frame_index)。
5. 视觉效果**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `svg()` 元素换颜色不产生新的图集瓦片，换尺寸会？
**答案**：图集键是 `RenderSvgParams { path, size }`（[src/window.rs:L4437-L4452](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/window.rs#L4437-L4452)），颜色只出现在最终 `MonochromeSprite` 的着色上，不参与键；尺寸决定光栅化结果，必须进键。

**练习 2**：`img()` 显示 SVG 与 `svg()` 元素显示 SVG 各适合什么场景？
**答案**：`img()` 适合把 SVG 当普通图片（多色插画、参与 object_fit/灰度/图墙布局），一次光栅化后纹理缩放；`svg()` 适合单色图标——按显示尺寸 2 倍超采样光栅化、`text_color` 着色、随主题变色零成本。

**练习 3**：一个 20000px 宽的 SVG 请求渲染会发生什么？
**答案**：`rasterize_tree` 检测超过 `MAX_SIZE = 8192` 后 warn 并按比例缩回上限（[src/svg_renderer.rs:L287-L295](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/svg_renderer.rs#L287-L295)），不会 panic，但显示分辨率受限于缩回后的瓦片。

## 5. 综合实践

把 `examples/image/image.rs` 改造成一个「资源体系试验台」，覆盖本讲全部四个最小模块：

1. **准备**：复制 `examples/image/image.rs` 为 `examples/image/my_assets.rs`，并在 `Cargo.toml` 的 `[[example]]` 区新增对应条目（参考现有 `name = "image"` 的写法，[Cargo.toml:L175-L177](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/Cargo.toml#L175-L177)）。示例已有 `Assets { base: examples 目录 }` 资产源——这就是 **AssetSource** 模块。
2. **本地图**：保留 `local_resource`（EXIF 旋转样例图，走 `Resource::Path`）。
3. **远程图 + 加载态**：给 `remote_resource` 那个 `img()` 加上 `.with_loading(...)`（抄 [examples/image_loading.rs:L73-L83](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/examples/image_loading.rs#L73-L83) 的呼吸动画）与 `.with_fallback(...)`，慢网下观察 200ms 延迟后才出现加载态——这是 **Img 元素** 模块。
4. **多尺寸同一 SVG**：加一行三个 `svg().path("image/arrow_circle.svg")`，分别 `.size_4()/.size_8()/.size_16()` 并给不同 `text_color`；再并排一个 `img("image/arrow_circle.svg").size_16()` 对比清晰度——这是 **SvgRenderer** 模块。
5. **缓存验证**：给最外层 div 挂 `.image_cache(retain_all("my-cache"))`（`retain_all` 接收一个元素 id，每帧重新构造 provider 即可，真正的缓存实体存在元素状态里跨帧复用，见 [src/elements/image_cache.rs:L339-L353](https://github.com/zed-industries/zed/blob/fe9556a11e9cc9dbc78686041aa524d6932879db/crates/gpui/src/elements/image_cache.rs#L339-L353)）；再给远程图加 `.on_click` 里调用 `cx.remove_asset::<ImgResourceLoader>(&resource)`，点击后观察图片重走加载流程。不点击时反复触发重绘（比如加一个每秒 `cx.notify()` 的定时器），图片不再闪加载态，说明命中了缓存——这是 **ImageCache** 与 **fetch_asset 去重** 模块。
6. **预期结果**：一个窗口里同时呈现三种资源来源、加载/失败替身、同源多尺寸 SVG 与缓存逐出重载。

运行表现需本地具备网络与 GPUI 编译环境，**待本地验证**。

## 6. 本讲小结

- `AssetSource` 回答「资源在哪」，`Resource` 三形态（Uri/Path/Embedded）统一寻址；不调 `with_assets` 时用空实现，内嵌资源一律查不到。
- `Asset` 协议 + `App::fetch_asset` 提供「(类型, 哈希) 只加载一次」的全局去重缓存；`use_asset` 完成后下一帧通知当前视图重绘，`get_asset` 不通知；该表永不淘汰。
- `img()` 在 `request_layout` 阶段完成加载触发、`aspect_ratio`/Auto 尺寸推导、动图帧推进与 loading/fallback 替身决策（200ms 阈值防闪烁）；`paint` 阶段按 `ObjectFit` 落位并按 `(ImageId, frame_index)` 复用图集瓦片。
- `ImageCache` 体系把淘汰权交给应用：`image_cache()` 元素或 `div().image_cache()` 为子树压栈，`RetainAllImageCache` 实体释放时自动 `drop_image`，示例的 `SimpleLruCache` 展示了自定义 LRU 的完整写法。
- `SvgRenderer = usvg 解析（含字体）+ resvg 光栅化`，固定 2 倍超采样、8192px 上限；`svg()` 元素缓存键是 (path, size) 的 alpha 蒙版、`text_color` 着色；`img()` 显示 SVG 则一次光栅化按位图管线走。
- 排查「不显示」：没配 AssetSource、`svg()` 没设 `text_color`、URL 非 2xx（看 `BadStatus`）、格式猜不出且 SVG 解析失败（看 `Usvg` 变体）。

## 7. 下一步学习建议

- 下一讲 u6-l8 转向**无障碍**：`window/a11y.rs` 如何在绘制阶段同步构建 AccessKit 树，`img`/`svg` 这类内容元素的无障碍语义如何标注。
- 想深入图集与渲染：阅读 `src/platform.rs` 中 `PlatformAtlas`/`SpriteAtlas` 相关 trait，弄清瓦片的分配与 `drop_image` 的回收路径（本讲的下游）。
- 想看真实工程的资源体系：对照 Zed 主程序里 `with_assets` 的实际装配（`zed` crate 的启动代码），体会「打包资源 vs 文件系统」两种 AssetSource 的取舍。
- 复习向：u3-l5（元素三阶段直觉）与本讲互为表里；u2-l5 的 `Shared<Task>` 语义在 `ImageCacheItem::Loading` 中再次出现，值得回看。
