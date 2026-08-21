# 示例全景：把 examples 当学习地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `examples/` 目录下 40 多个可运行示例的官方分类方式，并知道每一类的代表示例。
2. 掌握「按需查展示例」的方法：想学某个 API 时，能快速定位到演示它的那个示例，而不是从零写代码试错。
3. 读懂所有示例共享的统一代码骨架（`run_example` / `main` / wasm `start` 三段式），拿到任何示例都能在 1 分钟内找到「界面画在哪里」。
4. 养成「改一点、跑一次」的源码学习习惯：把示例当作可以随意修改的实验台。
5. 知道官方 README 的示例索引可能与目录不同步，学会用 `Cargo.toml` 和目录列表交叉验证。

## 2. 前置知识

本讲不需要新的框架知识，但依赖以下两点：

**（1）cargo 的 example 机制。** Rust 项目中放在 `examples/` 目录下的每个顶层 `.rs` 文件都会被 cargo 自动识别为一个独立可运行的示例（一个 example target），用 `cargo run --example <文件名>` 运行。如果示例入口文件放在子目录里、名字又不叫 `main.rs`，就需要在 `Cargo.toml` 里用 `[[example]]` 段显式声明名字和路径。gpui 的 `Cargo.toml` 中就有这样一段声明，我们在 4.1 节会看到它揭示的一个有趣事实。

**（2）u1-l2 学过的启动四步曲。** 每个示例的骨架都是同一套流程：

- `application().run(...)` 进入平台事件循环；
- `cx.open_window(WindowOptions, ...)` 打开窗口；
- 在开窗回调里 `cx.new(...)` 创建根视图实体；
- 根视图实现 `Render`，用 `div()` 声明界面。

本讲不再重复解释这四步，而是专注于「示例之间有什么不同、每个示例额外展示了什么 API」。

另外，`testing` 示例的测试部分需要开启 `test-support` feature 才能编译运行，这是 gpui 在 `Cargo.toml` 中定义的编译开关，本讲 4.5 节会用到。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/README.md` | 官方示例索引：运行命令 + 六大分区分类，是本讲的「地图本体」 |
| `examples/uniform_list.rs` | 虚拟化列表最小示例（65 行），官方推荐的新手起点之一 |
| `examples/input.rs` | 文本输入全集：动作、键位绑定、焦点、剪贴板、自定义 Element |
| `examples/testing.rs` | 双重身份示例：既是可运行的计数器应用，又内嵌一整套 GPUI 测试写法 |
| `examples/window.rs` | 窗口行为全集：四种 WindowKind、自定义标题栏、隐藏/缩放/对话框 |
| `Cargo.toml`（辅助） | `[[example]]` 声明段与 `test-support` feature，用于交叉验证示例清单 |

## 4. 核心概念与源码讲解

### 4.1 examples/README：官方示例索引的读法

#### 4.1.1 概念说明

`examples/README.md` 是 gpui 团队自己维护的示例目录。它回答两个问题：

1. **怎么跑**：所有示例都用同一条命令模板从仓库根目录运行。
2. **怎么选**：把示例按「新手起点 / 布局样式 / 交互 / 图片绘制动画 / 窗口行为 / 专用」六个分区归类，每个示例配一句话说明它演示什么。

这份 README 是学习 GPUI 最高效的入口：它本质上是一张「API → 演示程序」的反向索引表。当你后续讲义里学到某个 API（比如 `deferred`、`anchored`、`KeyBinding`），回来查一下 README 就能找到演示它的最小可运行程序。

#### 4.1.2 核心流程

README 的分区结构可以浓缩为一张表（四大导览类 + 两个特殊分组）：

| README 分区 | 对应本讲分类 | 收录的示例 | 数量 |
| --- | --- | --- | --- |
| Where to start | 新手起点 | `hello_world`、`input`、`uniform_list`、`testing` | 4 |
| Layout and styling | 布局样式 | `grid_layout`、`opacity`、`pattern`、`shadow`、`text`、`text_layout`、`text_wrapper` | 7 |
| Interaction | 交互 | `anchor`、`data_table`、`drag_drop`、`focus_visible`、`mouse_pressure`、`popover`、`scrollable`、`tab_stop` | 8 |
| Images, drawing, and animation | 图片绘制动画 | `animation`、`gif_viewer`、`gradient`、`image`、`image_gallery`、`image_loading`、`painting`、`svg` | 8 |
| Windows and application behavior | 窗口行为 | `move_entity_between_windows`、`on_window_close_quit`、`set_menus`、`system_notifications`、`window`、`window_positioning`、`window_shadow` | 7 |
| Specialized | 专用（调试框架本身用） | `active_state_bug`、`layer_shell`、`list_example`、`ownership_post`、`paths_bench`、`tree` | 6 |

使用流程建议：

1. 想了解某个能力 → 在 README 里按分区找到对应示例名。
2. `cargo run -p gpui --example <示例名>` 运行观察现象。
3. 打开对应 `examples/<示例名>.rs`，通常只有几十到几百行，从 `render()` 或 `run_example()` 读起。
4. 修改一处代码，重新运行对比差异。

#### 4.1.3 源码精读

运行命令写在 README 开头，注意它声明了「要在 Zed 仓库根目录下执行」：

[examples/README.md:1-7](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md#L1-L7) —— 给出统一的运行模板 `cargo run -p gpui --example hello_world`。任何示例都只需把 `hello_world` 换成对应名字。

新手起点分区：

[examples/README.md:9-17](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md#L9-L17) —— 官方点名的四个起点：`hello_world`（应用基本形状）、`input`（文本输入/焦点/选区/剪贴板/键位）、`uniform_list`（虚拟化列表）、`testing`（测试基础设施）。本讲 4.3–4.5 节会精读其中三个（`hello_world` 已在 u1-l2 逐行讲过）。

其余四个分区：

- [examples/README.md:19-27](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md#L19-L27) —— 布局样式类：网格布局、透明度、图案背景、阴影、样式化文本、文本对齐与换行。
- [examples/README.md:29-39](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md#L29-L39) —— 交互类：锚定定位、表格、拖放、键盘可见焦点、压感指针、悬浮层、滚动、Tab 导航。
- [examples/README.md:41-50](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md#L41-L50) —— 图片绘制动画类：动画与 SVG 变换、GIF 渲染、渐变与色彩空间、本地图/远程图加载、图片缓存、路径自绘、SVG 渲染。
- [examples/README.md:52-60](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/README.md#L52-L60) —— 窗口行为类：实体跨窗口迁移、关窗即退出、应用菜单、系统通知、四种窗口形态、窗口定位、窗口阴影。

还有一个值得注意的事实：**README 并没有收录全部示例**。目录下实际存在 `a11y.rs`（无障碍）和 `window_movable.rs`（窗口拖动）两个顶层示例，以及一个多文件示例 `view_example/`，它们都没有出现在 README 的任何分区里，但却被 `Cargo.toml` 显式声明为可运行的 target：

- [Cargo.toml:197-199](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L197-L199) —— 声明 `window_movable` 示例。
- [Cargo.toml:262-264](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L262-L264) —— 声明 `a11y` 示例。
- [Cargo.toml:266-268](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L266-L268) —— 声明 `view_example`，入口在子目录 `examples/view_example/view_example_main.rs`。

这说明**索引文档可能滞后于代码**。统计一下：顶层 40 个 `.rs` 示例 + 子目录里的 `image`、`svg`、`view_example` 三个，共 **43 个示例 target**，README 收录了其中 40 个。为什么 `image`、`svg`、`view_example` 必须在 Cargo.toml 里声明？因为 cargo 的自动发现只认 `examples/*.rs`（顶层单文件）和 `examples/*/main.rs`（子目录惯例名），而它们的入口文件分别是 `image/image.rs`、`svg/svg.rs`、`view_example/view_example_main.rs`，不符合惯例名，必须显式指定 path。

#### 4.1.4 代码实践

**实践目标**：验证 README 索引与实际目录的一致性，找出「隐藏」的示例。

**操作步骤**：

1. 列出目录下所有顶层示例文件（在 `crates/gpui/` 下执行）：
   ```sh
   ls examples/*.rs
   ```
2. 打开 `examples/README.md`，把六个分区里提到的示例名抄成一份清单。
3. 对比两份清单，找出「目录里有、README 里没有」的示例名。
4. 再打开 `Cargo.toml` 的 `[[example]]` 段（约 177 行起），确认这些遗漏的示例确实被声明为可运行 target。
5. 任选一个「README 未收录」的示例（如 `a11y`）运行：`cargo run -p gpui --example a11y`。

**需要观察的现象**：目录清单比 README 清单多出 `a11y`、`window_movable` 两个名字；Cargo.toml 里能找到它们的声明。

**预期结果**：你会得到一份「比官方索引更全」的示例清单。同时体会一个通用的源码阅读原则——**文档会过期，目录和构建配置不会**。

**说明**：步骤 1–4 是纯文件阅读，随时可做；步骤 5 需要本地图形环境（Linux 上需要可用的 X11/Wayland 会话），在无显示的服务器上会运行失败，属于「待本地验证」项。

#### 4.1.5 小练习与答案

**练习 1**：README 的 Specialized 分区里，哪个示例是为配合 gpui 的官方所有权文档而写的？

**答案**：`ownership_post`——它支撑 ownership and data-flow 文档（该文档即 u2 将要精读的 `_ownership_and_data_flow.rs` 对应的示例）。

**练习 2**：为什么 `image`、`svg`、`view_example` 三个示例必须在 `Cargo.toml` 中用 `[[example]]` 显式声明，而 `testing`、`window` 不用？

**答案**：前三个的入口文件在子目录中且不叫 `main.rs`，cargo 自动发现规则覆盖不到；`testing.rs`、`window.rs` 是 `examples/` 下的顶层单文件，会被自动发现。

**练习 3**：如果你想看「滚动条 + 表格 + 虚拟化」组合的示例，应该运行哪一个？

**答案**：`data_table`（Interaction 分区，README 对它的说明是「结合虚拟化列表渲染、表格风格行与自定义滚动条」）。

### 4.2 所有示例共享的代码骨架

#### 4.2.1 概念说明

43 个示例看起来五花八门，但它们的「外壳」完全一致。掌握这个统一骨架后，你打开任何一个示例文件都能立刻定位到关键位置，不被入口代码干扰。骨架由三部分组成：

1. **`run_example()` 函数**：装着 u1-l2 学过的启动四步曲，是示例真正的入口逻辑。
2. **`#[cfg(not(target_family = "wasm"))] fn main()`**：原生平台（macOS/Linux/Windows）的入口，只做一件事——调用 `run_example()`。
3. **`#[cfg(target_family = "wasm")] pub fn start()`**：WebAssembly 环境的入口，先调用 `gpui_platform::web_init()` 再调用同一个 `run_example()`。

这样设计的动机：同一份示例代码既能编译成桌面程序，也能编译成网页版（GPUI 支持 wasm 后端），两个平台共享全部业务逻辑，只在「谁来启动」这一步分叉。

#### 4.2.2 核心流程

```
编译目标判断
├── 原生平台：main() ──────────────┐
│                                   ├──→ run_example()
│                                      │  1. application().run(...)   进入事件循环
│                                      │  2. cx.open_window(...)      开窗
│                                      │  3. cx.new(...)              创建根视图
│                                      │  4. impl Render              声明界面
└── wasm：start() → web_init() ─────┘
```

读示例时的最短路径：**先跳到 `impl Render`**（界面长什么样、用了什么元素），**再看 `run_example()` 里 `open_window` 之前做了什么配置**（绑了哪些键、注册了哪些动作），这两处就是示例想教你的全部内容。

#### 4.2.3 源码精读

以 `uniform_list.rs` 为例看这套骨架（它是最短的示例之一，骨架占比最高）：

[examples/uniform_list.rs:41-53](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/uniform_list.rs#L41-L53) —— `run_example()`：`application().run` 开启事件循环，`Bounds::centered` 计算居中窗口位置，`cx.open_window(WindowOptions { window_bounds: ..., ..Default::default() }, ...)` 开一个 300×300 的窗口，回调里 `cx.new(|_| UniformListExample {})` 创建根视图。这就是启动四步曲的标准形状。

[examples/uniform_list.rs:55-58](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/uniform_list.rs#L55-L58) —— 原生入口：仅当编译目标不是 wasm 时编译 `main`，调用 `run_example()`。

[examples/uniform_list.rs:60-65](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/uniform_list.rs#L60-L65) —— wasm 入口：`#[wasm_bindgen]` 导出 `start()`，先 `gpui_platform::web_init()` 初始化 web 后端，再复用同一个 `run_example()`。文件第 1 行的 `#![cfg_attr(target_family = "wasm", no_main)]` 则告诉编译器 wasm 目标下不要生成标准 `main`。

再看一个入口逻辑更丰富的例子，`input.rs` 的启动配置（骨架相同，但 `run_example()` 里多了内容）：

[examples/input.rs:697-714](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L697-L714) —— `run_example()` 开头先 `cx.bind_keys([...])` 把 backspace、方向键、cmd-a 等按键绑定到动作上，这就是「示例想教你的额外内容」出现在骨架缝线里的典型位置。

#### 4.2.4 代码实践

**实践目标**：体会「三段式骨架」在多个示例中的重复出现，并验证修改入口逻辑的效果。

**操作步骤**：

1. 打开 `examples/uniform_list.rs`、`examples/input.rs`、`examples/window.rs`、`examples/testing.rs`，分别跳到文件末尾，确认四个文件都有 `run_example()` / `main` / wasm `start` 三个部分。
2. 修改 `examples/uniform_list.rs`（建议先复制一份再改，或改完还原），在 `run_example()` 的 `application().run` 回调第一行加一句：
   ```rust
   println!("GPUI 事件循环已启动");
   ```
3. 运行 `cargo run -p gpui --example uniform_list`（在仓库根目录执行）。
4. 观察终端输出与窗口出现的先后顺序。

**需要观察的现象**：终端先打印出日志，随后窗口出现——因为回调在事件循环启动后、开窗之前执行。

**预期结果**：日志在窗口渲染前打印一次且仅一次。若在无图形环境的服务器上运行，窗口创建会失败，此实践「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把示例逻辑直接写在 `main()` 里，而要包一层 `run_example()`？

**答案**：因为原生平台和 wasm 两个编译目标需要不同的入口函数（`main` 与 wasm_bindgen 的 `start`），抽出 `run_example()` 才能让两边共享同一份逻辑。

**练习 2**：`#![cfg_attr(target_family = "wasm", no_main)]` 这行文件级属性的作用是什么？

**答案**：仅在编译目标是 wasm 时给整个文件加上 `no_main` 属性，禁用标准 Rust `main` 入口——此时入口改由 wasm_bindgen 导出的 `start()` 承担。

### 4.3 新手起点与虚拟化列表：uniform_list

#### 4.3.1 概念说明

`uniform_list` 是官方点名的四个新手起点之一，也是最「小而完整」的示例：整个文件只有 65 行，却演示了 GPUI 里非常重要的一个模式——**虚拟化列表**。

所谓虚拟化，是指列表不一次性创建全部条目的 UI 元素，而是**只创建当前滚动位置可视区间内的条目**。想象一个有 10 万行的列表：如果每行都是一个 `div`，一次性全部创建会把 CPU 和内存压垮；虚拟化后，任何时刻内存里只有视口覆盖的那几十行。这是 Zed 编辑器能流畅渲染超长文件、超长符号列表的基础技术之一（u6-l1 将深入其实现源码）。

「uniform」（统一）指列表中所有条目高度相同——这正是它能简单实现虚拟化的前提：知道行高和滚动偏移，就能直接算出可视区间是第几行到第几行，无需逐条测量。

#### 4.3.2 核心流程

```
uniform_list(列表 id, 条目总数, 渲染回调)
   │
   ├─ 框架根据视口大小 + 滚动偏移算出当前可视行号区间 range
   ├─ 对 range 内每个行号调用回调，回调返回该行的元素（div）
   └─ 视口外滚动 → 重新计算 range → 回调重新执行 → 只重建可见行
```

关键认知：回调的参数 `range` 由框架传入，你写的代码只负责「把一段行号区间变成一批元素」。

#### 4.3.3 源码精读

[examples/uniform_list.rs:11-39](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/uniform_list.rs#L11-L39) —— `UniformListExample` 的完整 `Render` 实现，也是这个示例的全部界面代码。根 `div` 铺满窗口（`.size_full()`）作为白色背景，唯一的孩子就是 `uniform_list(...)` 的返回值。

[examples/uniform_list.rs:14-35](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/uniform_list.rs#L14-L35) —— `uniform_list` 的三个参数：字符串 `"entries"`（列表标识）、`50`（条目总数）、`cx.processor(...)` 渲染回调。回调签名为 `|_this, range, _window, _cx|`，其中 `range` 是当前可视的行号区间；函数体遍历区间内每个行号 `ix`，构造一个带 `.id(ix)`、内边距、指针光标和 `on_click` 回调的 `div`，显示 `Item {ix+1}`，点击时在终端打印行号。注意每个条目 div 都调用了 `.id(ix)`——给元素一个稳定标识，这是可交互（有状态）元素的必要条件，u5-l2 会解释原因。

[examples/uniform_list.rs:17-34](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/uniform_list.rs#L17-L34) —— 回调内部：`for ix in range` 循环构造条目向量并返回。返回类型是 `Vec` 条目，框架会把这些条目按顺序放进列表。

这个示例演示的核心 API 清单：`uniform_list()` 构造函数、`cx.processor()` 包装渲染回调、条目元素 `.id()` / `.on_click()`。

#### 4.3.4 代码实践

**实践目标**：直观感受虚拟化——把条目总数从 50 提到 10 万，验证界面依然流畅。

**操作步骤**：

1. 复制 `examples/uniform_list.rs` 的思路（或直接在源码上修改后还原），把 `uniform_list("entries", 50, ...)` 中的 `50` 改为 `100_000`。
2. 运行 `cargo run -p gpui --example uniform_list`。
3. 用鼠标滚轮快速从头滚到尾。
4. 点击任意一行，观察终端输出。

**需要观察的现象**：列表可以滚动浏览全部 10 万行；无论怎么滚动，界面不应出现明显卡顿；点击的行号会打印在终端。

**预期结果**：滚动流畅、点击正常——因为任何时刻实际创建的只有可视区的几十个条目元素。这就是虚拟化的价值。（界面流畅度依赖本机图形环境，「待本地验证」。）

#### 4.3.5 小练习与答案

**练习 1**：`uniform_list` 的第二个参数 `50` 表示什么？

**答案**：列表条目的总数量。框架需要知道总数才能计算滚动范围，但它不会为不可见的条目创建元素。

**练习 2**：为什么 `uniform_list` 能轻松虚拟化，而「每条消息高度都不同」的聊天列表需要更复杂的 `list` 元素？

**答案**：等高列表知道行高后可以直接用「偏移 ÷ 行高」算出可视行号区间；变高列表必须先测量（或估算）每条的高度才能定位，gpui 为此提供了另一个带高度缓存的 `list` 元素（见 `examples/list_example.rs` 与 u6-l2）。

**练习 3**：如果把回调里条目 div 的 `.id(ix)` 去掉，会发生什么？

**答案**：`on_click` 属于有状态交互能力，要求元素有稳定 id；去掉后该构造在编译期就会报错（`on_click` 定义在 `StatefulInteractiveElement` trait 上，仅对 `Stateful<E>` 即有 id 的元素可用）。这是 u5-l2 的内容，此处先记住结论即可。

### 4.4 交互类代表：input

#### 4.4.1 概念说明

`input` 是交互分区的集大成者：它实现了一个真正可用的单行文本输入框，涉及 GPUI 交互体系的几乎全部子系统——

- **动作（Action）**：把「退格」「全选」「粘贴」等操作定义为类型；
- **键位绑定（KeyBinding）**：把物理按键映射到动作；
- **焦点（Focus）**：决定键盘输入流向哪个元素；
- **剪贴板**：复制/剪切/粘贴；
- **自定义 Element**：光标、选区高亮不能用现成组件，需要手写三阶段绘制；
- **输入法（IME）对接**：实现 `EntityInputHandler` 供平台层调用。

它同时是「新手起点」分区里公认最难的一个——但作为**查阅清单**价值极高：你以后写任何交互组件，几乎都能在 `input.rs` 里找到对应写法的原型。本讲只做导览，帮你知道「哪段代码对应哪个知识点、将来去哪讲深入」；每个子系统的原理分别属于 u5（事件/动作/焦点）和 u6（文本系统）。

#### 4.4.2 核心流程

`input` 示例中一次按键的完整旅程：

```
用户按下 "backspace"
  → 平台产生 keystroke
  → Keymap 查 KeyBinding：backspace → Backspace 动作
  → 沿焦点链派发动作到聚焦的 TextInput
  → div 上注册的 on_action 处理器命中 Self::backspace
  → 方法内修改 selected_range/content 并 cx.notify()
  → 下一帧重绘，光标/文本更新
```

而文字的直接键入走的是另一条路：平台输入系统（含输入法）→ `EntityInputHandler::replace_text_in_range` → 修改内容 → 重绘。

#### 4.4.3 源码精读

[examples/input.rs:16-34](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L16-L34) —— 用 `actions!` 宏在 `text_input` 命名空间下一次性定义 `Backspace`、`Delete`、`SelectAll`、`Paste`、`Cut`、`Copy` 等 14 个动作类型。动作是 GPUI 把「按键」翻译成「语义操作」的中介（u5-l3 精讲）。

[examples/input.rs:700-714](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L700-L714) —— 在 `run_example()` 里 `cx.bind_keys([...])` 建立按键到动作的映射表：`backspace` 键 → `Backspace` 动作、`cmd-a` → `SelectAll`、`cmd-v` → `Paste` 等（u5-l4 精讲派发链路）。

[examples/input.rs:585-621](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L585-L621) —— `TextInput` 视图的 `render()`：在根 div 上依次挂 `.key_context("TextInput")`（声明键位上下文）、`.track_focus(&self.focus_handle(cx))`（参与焦点链）、13 个 `.on_action(cx.listener(Self::xxx))`（动作处理器）和 4 个鼠标监听。这个「div + on_action + cx.listener」的组合是 GPUI 交互组件的标准写法。

[examples/input.rs:419-441](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L419-L441) —— 手写 `TextElement` 的 `request_layout` 阶段：向布局引擎申报「宽度撑满、高度为一行行高」。这是自定义 Element 三阶段（request_layout → prepaint → paint）的第一阶段。

[examples/input.rs:444-540](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L444-L540) —— `prepaint` 阶段：读取实体状态，用 `window.text_system().shape_line(...)` 对文本做整形（shaping）得到 `ShapedLine`，再据此计算光标和选区高亮的矩形位置，存进 `PrepaintState` 供 paint 使用。

[examples/input.rs:542-583](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L542-L583) —— `paint` 阶段：先 `window.handle_input(...)` 把自己注册为当前焦点位置的输入处理者，再依次绘制选区矩形、文本行、光标，最后把 `ShapedLine` 和边界写回实体，供鼠标点击换算字符偏移使用。

[examples/input.rs:743-755](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L743-L755) —— `cx.observe_keystrokes(...)`：界面下半部分「最近按键」列表的数据来源，每按一个键就把它记录进 `recent_keystrokes` 并刷新界面。

#### 4.4.4 代码实践

**实践目标**：通过运行和修改，验证「按键 → 动作 → 处理器」链路，并留下一份按键现象记录。

**操作步骤**：

1. 运行 `cargo run -p gpui --example input`（仓库根目录）。
2. 在输入框里打字，用鼠标拖选一段文字，分别按 `cmd-c`（复制）、`cmd-v`（粘贴）、`backspace`、`home`、`end`，观察行为。
3. 观察窗口下方的「最近按键」列表：每按一个键，最新的一条出现在最上面（代码里用了 `.iter().rev()`）。
4. 修改一条键位绑定：把 [examples/input.rs:712](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L712) 中的 `"home"` 改为 `"ctrl-h"`，重新运行，验证 `Home` 键失效、`Ctrl-H` 让光标跳到开头。
5. 改完把源码还原。

**需要观察的现象**：步骤 2 中每个按键都触发对应的编辑动作；步骤 4 中改绑后行为跟着键位走。

**预期结果**：`ctrl-h` 触发光标移动到开头，`home` 键不再有反应。（本示例需要图形环境与键盘输入，「待本地验证」。）

#### 4.4.5 小练习与答案

**练习 1**：`input.rs` 中 `actions!` 宏定义的动作和 `cx.bind_keys` 注册的绑定，分别解决什么问题？

**答案**：`actions!` 定义「能做什么」（语义操作类型，与按键无关）；`bind_keys` 定义「按什么键触发哪个操作」（可配置的映射）。两者分离让同一套动作可以按平台习惯绑定不同按键。

**练习 2**：为什么 `TextElement` 要在 `prepaint` 里做文本整形，而不是在 `paint` 里？

**答案**：三阶段分工是「request_layout 申报尺寸 → prepaint 拿到最终 bounds 并准备绘制所需数据 → paint 真正发出绘制指令」。文本整形需要知道字体与内容但不依赖 bounds 的部分可提前，而整形结果（光标 x 坐标等）要和 bounds 相加才能得到屏幕坐标；把准备工作放 prepaint、把绘制放 paint 是 GPUI 的固定节奏，u4-l1 会系统讲解。

**练习 3**：示例界面顶部显示 `Keyboard <布局名>`，这条信息从哪来？

**答案**：`render()` 中调用 `cx.keyboard_layout().name()`（见 [examples/input.rs:666](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/input.rs#L666)），且 `run_example()` 里注册了 `cx.on_keyboard_layout_change` 在布局变化时刷新界面。

### 4.5 窗口行为与测试双雄：window 与 testing

#### 4.5.1 概念说明

这一节把两个示例放在一起讲，因为它们代表示例地图上两个特殊方向：

- **`window`** 是「窗口行为」类的代表。它像一个窗口特性试验台：一个界面上排满了按钮，每颗按钮用一组不同的 `WindowOptions` 打开一个新窗口，集中演示 GPUI 对窗口形态的控制能力（普通/弹出/浮动/对话框窗口、自定义标题栏、隐藏窗口、禁止移动/缩放/最小化等）。
- **`testing`** 展示了示例的另一种用法：**同一份代码既能 `cargo run` 当应用跑，又能 `cargo test` 当测试跑**。它的上半部分是一个普通的计数器应用，下半部分（`#[cfg(test)]` 模块）则是一份几乎涵盖 GPUI 测试基础设施全部写法的活教材——从基础实体测试、窗口测试、异步测试到属性测试、甚至模拟分布式系统。

对初学者来说，`window` 教你「GPUI 能对窗口做什么」，`testing` 教你「怎么验证自己写的 GPUI 代码是对的」。

#### 4.5.2 核心流程

`window` 示例的结构：

```
WindowDemo（主窗口，800×600）
  └─ 一排按钮，每颗按钮 = 一种 WindowOptions 组合
       ├─ Normal        → 默认选项开窗
       ├─ Popup/Floating/Dialog → kind: WindowKind::PopUp / Floating / Dialog
       ├─ Custom Titlebar → titlebar: None + 自己画标题栏
       ├─ Invisible    → show: false
       ├─ Unmovable / Unresizable / Unminimizable → is_*: false
       ├─ Hide Application → cx.hide() + 3 秒后 cx.activate(false) 恢复
       ├─ Resize       → window.resize() 交换宽高
       └─ Prompt       → window.prompt() 弹原生对话框并 await 答案
```

`testing` 示例的双重身份：

```
cargo run -p gpui --example testing            → 跑 run_example()：交互计数器
cargo test -p gpui --example testing --features test-support
                                               → 跑 #[cfg(test)] 模块：六个测试
```

#### 4.5.3 源码精读

**window.rs：**

[examples/window.rs:14-27](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L14-L27) —— 辅助函数 `button()`：一个带 `.id()`、悬停/按下反馈和 `on_click` 的通用按钮。这个函数是「把重复 UI 封装成普通函数」的最简单方式（比组件化更轻，u3-l6 会讲更正式的 `RenderOnce` 组件）。

[examples/window.rs:122-137](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L122-L137) —— 「Popup」按钮：`WindowOptions` 中设置 `kind: WindowKind::PopUp` 打开弹出式窗口。「Floating」（[L138-L153](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L138-L153)）和「Dialog」（[L154-L169](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L154-L169)）同理，分别用 `WindowKind::Floating` 和 `WindowKind::Dialog`。加上默认的普通窗口（[L107-L121](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L107-L121)），四种窗口形态一次看全。

[examples/window.rs:170-185](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L170-L185) —— 「Custom Titlebar」按钮：`titlebar: None` 去掉系统标题栏，然后子视图 `SubWindow` 自己用一个蓝色 `div` 画标题栏（见 [L40-L58](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L40-L58) 的 `.when(self.custom_titlebar, ...)` 条件渲染）。这是 Zed 定制标题栏效果的基础。

[examples/window.rs:186-218](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L186-L218) —— 「Invisible」（`show: false`，窗口创建但不显示）与「Unmovable」（`is_movable: false`）两个开关；紧随其后的还有 `is_resizable: false`（[L219-L234](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L219-L234)）与 `is_minimizable: false`（[L235-L250](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L235-L250)）。`WindowOptions` 的能力一目了然。

[examples/window.rs:251-265](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L251-L265) —— 「Hide Application」：`cx.hide()` 隐藏整个应用，然后 `window.spawn` 一个异步任务，用 `background_executor().timer(...)` 等 3 秒再 `cx.activate(false)` 恢复显示。展示了应用级显隐与定时器的配合。

[examples/window.rs:270-287](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L270-L287) —— 「Prompt」：`window.prompt(PromptLevel::Info, "Are you sure?", None, &["OK", "Cancel"], cx)` 返回一个可以 `await` 的响应，用户点击的按钮以索引返回（0 表示 OK）。原生对话框在 GPUI 里是一个异步 API（u7-l3 精讲）。

[examples/window.rs:311-337](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/window.rs#L311-L337) —— `run_example()`：开窗回调里用 `cx.observe_window_bounds(...)` 订阅窗口尺寸变化并打印日志，末尾 `cx.activate(true)` 把应用带到前台，还注册了 `cmd-q` 退出。

**testing.rs：**

[examples/testing.rs:1-8](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L1-L8) —— 文件级文档注释，明确写出这个示例的两种运行方式，尤其是测试命令需要 `--features test-support`（该 feature 定义于 [Cargo.toml:21-27](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/Cargo.toml#L21-L27)）。

[examples/testing.rs:16-27](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L16-L27) —— 应用部分同样是标准骨架：`actions!` 定义 `Increment`/`Decrement` 动作，`Counter` 实体持有计数值、焦点句柄和一个自订阅（`cx.subscribe_self`，收到自己发出的事件就把计数改成 999——这是专为测试副作用时序设计的）。

[examples/testing.rs:80-99](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L80-L99) —— `render()` 开头：`.key_context("Counter")` + 两个 `.on_action(cx.listener(...))`。配合 `run_example()` 里把 `up`/`down` 键绑定到动作且上下文限定为 `Some("Counter")`（[L182-L185](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L182-L185)），只有焦点在 Counter 内时方向键才生效。

[examples/testing.rs:224-246](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L224-L246) —— 最基础的 `#[gpui::test]`：不需要窗口，直接创建实体、`update` 修改、`read_with` 断言。注释特意说明 `TestAppContext` 不支持 `read(cx)`，要用 `read_with`；并演示「同步副作用在 update 结束后立即执行」。

[examples/testing.rs:251-273](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L251-L273) —— 涉及窗口的测试：用 `VisualTestContext::from_window` 构造带窗口的上下文，然后通过 `focus_handle.dispatch_action(&Increment, window, cx)` 模拟动作派发并断言计数变化。

[examples/testing.rs:278-299](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L278-L299) —— 异步测试：测试执行器是单线程的，被 detach 的任务不会自己运行，必须显式 `cx.run_until_parked()` 推进——断言从 100（reload 前）到 150（park 后）的变化把这个时序展示得很清楚。

[examples/testing.rs:326-357](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L326-L357) —— 属性测试：`#[gpui::test(iterations = 10)]` 让同一个测试用随机种子跑 10 遍，随机执行 100 次增减后核对结果。

[examples/testing.rs:470-487](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L470-L487) —— 最「炫」的用法：一个测试函数接收**两个** `TestAppContext` 参数，模拟两个进程通过 Mock 网络同步状态；配合 `iterations` 还能随机化两个 App 任务的执行顺序（[L493-L550](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/examples/testing.rs#L493-L550)），用于验证分布式代码对乱序的健壮性。

#### 4.5.4 代码实践

**实践目标**：跑通 `window` 的按钮矩阵和 `testing` 的双模式（运行 + 测试）。

**操作步骤**：

1. 运行 `cargo run -p gpui --example window`。
2. 依次点击主窗口中的按钮：`Popup`、`Dialog`、`Custom Titlebar`、`Unmovable`、`Prompt`，每种都观察新窗口与主窗口行为的差异；拖动主窗口边框，观察终端中 `observe_window_bounds` 打印的日志。
3. 运行 `cargo run -p gpui --example testing`，分别用鼠标点击 `+`/`−` 和键盘 `↑`/`↓`，验证两种触发路径都改变计数。
4. 在仓库根目录运行 `cargo test -p gpui --example testing --features test-support`。
5. 阅读终端输出的测试名列表，与 4.5.3 节列出的测试函数一一对应。

**需要观察的现象**：步骤 2 中 Dialog 窗口在窗口管理器中的行为与普通窗口不同（例如是否遮挡父窗口随平台而异）；Prompt 点击 OK 后终端打印 `You have clicked Ok`；步骤 3 中键盘仅在 Counter 聚焦时生效；步骤 4 中所有测试通过。

**预期结果**：`testing` 示例的 6 个左右测试全部通过（green）。运行类步骤依赖图形环境，「待本地验证」；步骤 4 的测试在无显示环境下通常也能运行（GPUI 测试平台不创建真实窗口），但同样以待本地验证为准。

#### 4.5.5 小练习与答案

**练习 1**：`window.rs` 里四种窗口形态分别通过 `WindowOptions` 的哪个字段设置？

**答案**：`kind` 字段，取值为 `WindowKind::Normal`（默认，如 Normal 按钮）、`WindowKind::PopUp`、`WindowKind::Floating`、`WindowKind::Dialog`。

**练习 2**：`window.prompt(...)` 的返回值为什么可以被 `await`？用户点击按钮前代码停在哪里？

**答案**：它返回一个 GPUI `Task`（异步任务），`await` 时当前异步任务挂起、控制权回到事件循环；用户点击对话框按钮后 Task 完成，`await` 处恢复，得到所点按钮的索引。

**练习 3**：在 `testing` 示例的异步测试里，如果不调用 `cx.run_until_parked()`，第二个断言会发生什么？

**答案**：detach 出去的 reload 任务不会执行，计数仍是 100 而不是 150，断言 `count_after == 150` 失败。原因：测试执行器是单线程的，被 detach 的任务必须等测试代码主动让出控制权（如 `run_until_parked`）才有机会运行。

## 5. 综合实践

**任务：制作你自己的「示例 → API 查阅清单」。** 这份清单将贯穿整个学习手册的使用过程——以后任何时候想不起某个 API 怎么用，先查它。

从布局样式、交互、图片绘制动画、窗口行为四大类中**各挑一个示例**（建议：`opacity` 或 `grid_layout`、`drag_drop`、`gradient` 或 `painting`、`window_positioning`），完成以下步骤：

1. **运行**：在仓库根目录用 `cargo run -p gpui --example <名字>` 逐个运行四个示例，操作界面，记下你看到的每个可交互现象。
2. **记录**：为每个示例填一行下表（示例代码顶部的 `use gpui::{...}` 导入列表是找核心 API 的捷径——它就是这个示例的「教学内容目录」）：

   | 类别 | 示例名 | 核心导入的关键 API | 界面上看到什么 | 相关源码位置 | 后续哪一讲深入 |
   | --- | --- | --- | --- | --- | --- |
   | 布局样式 | （示例代码，非项目原有内容） | | | | |
   | 交互 | | | | | |
   | 图片绘制动画 | | | | | |
   | 窗口行为 | | | | | |

3. **小改动**：在其中一个示例里做一处最小修改（改一个颜色、一个尺寸、一个键位），重新运行确认改动生效，然后还原源码。
4. **归档**：把这张表保存下来，并在「后续哪一讲深入」一列填上本手册对应讲义编号（可对照第 7 节的路标）。

**验收标准**：四个示例都运行过并留下记录；至少一处改动验证了「改一点跑一次」的工作流；清单中每行都能追溯到真实的运行现象。

**注意**：所有运行步骤都需要本地图形环境（X11/Wayland 会话、GPU 驱动）。如果在无显示的远程机器上，可先用 `cargo build -p gpui --examples` 验证全部示例能编译通过（此命令不需要显示环境），把运行部分留到本地机器完成。

## 6. 本讲小结

- `examples/` 目录共有 **43 个示例 target**（40 个顶层 `.rs` + 子目录中的 `image`、`svg`、`view_example`），`examples/README.md` 提供官方分类索引，但它收录 40 个——`a11y`、`window_movable`、`view_example` 未被收录，查索引时记得与目录和 `Cargo.toml` 交叉验证。
- README 的六大分区可归并为四大学习类：**布局样式、交互、图片绘制动画、窗口行为**，外加「新手起点」（`hello_world`、`input`、`uniform_list`、`testing`）与「专用」两个特殊分组。
- 所有示例共享同一套骨架：`run_example()`（启动四步曲所在）+ 原生 `main()` + wasm `start()`；读示例先跳 `impl Render`，再看 `run_example()` 里 `open_window` 之前的配置。
- `uniform_list` 用 65 行演示了等高虚拟化列表：框架把可视行号区间传给回调，回调只为可见行构造元素。
- `input` 是交互 API 的「百科全书示例」：动作定义、键位绑定、焦点、剪贴板、自定义 Element 三阶段绘制、输入法对接全部在这一个文件里。
- `window` 与 `testing` 展示两个特殊方向：前者是 `WindowOptions`/`WindowKind` 的按钮试验台，后者证明示例可以同时是应用和测试（`cargo test -p gpui --example testing --features test-support`）。

## 7. 下一步学习建议

本讲是单元 1（初识 GPUI）的最后一讲，你已经具备阅读任何示例的骨架知识。接下来进入**单元 2：状态与上下文**，建议按顺序：

1. **u2-l1（App 与 Application：应用生命周期）**——深入你在每个示例 `run_example()` 里看到的 `application().run(...)` 背后发生了什么。
2. **u2-l2（Entity 与所有权模型）**——理解示例中到处出现的 `cx.new(...)`、`Entity<T>`、`cx.entity()` 的底层机制；配合 `examples/ownership_post.rs`（本讲 Specialized 分区提到的示例）阅读效果更佳。
3. 在进入单元 2 之前，可以顺手运行 `examples/view_example`（README 未收录的多文件示例，`cargo run -p gpui --example view_example`），它展示了一个由多个视图模块组成的中等规模应用结构，正好作为「从单文件示例迈向真实应用」的过渡材料。

读完单元 2 后再回来重跑 `input` 与 `testing`，你会看懂当初跳过的 `cx.listener`、`cx.subscribe_self`、`cx.spawn` 每一行。
