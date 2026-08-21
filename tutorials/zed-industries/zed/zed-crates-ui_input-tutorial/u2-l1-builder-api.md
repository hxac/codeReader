# u2-l1 InputField 的数据模型与 Builder API

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐一说出 `InputField` 结构体全部 9 个字段（`label`、`label_size`、`placeholder`、`editor`、`start_icon`、`min_width`、`tab_index`、`tab_stop`、`masked`、`error`）的类型、含义与默认值。
2. 解释 `InputField::new` 如何通过 `ERASED_EDITOR_FACTORY` 创建底层编辑器、为什么占位文本要立刻推送给内芯，以及工厂未初始化时会发生什么。
3. 掌握 builder 风格链式 API 的惯用法：哪些配置在构造期用 `mut self -> Self` 设置，哪些状态在运行期用 `&mut self` + `cx` 修改。
4. 理解 `Focusable` 实现如何把焦点委托给内部 editor，以及 `text()` / `set_text()` / `clear()` / `is_empty()` 这组委托方法为什么只接收 `&self`。

本讲只看「数据模型与 API」，不深入 `render` 的布局细节——那是下一讲 u2-l2 的主题。

## 2. 前置知识

### 2.1 Entity 与 cx.new

在 GPUI 中，`Entity<T>` 是指向状态 `T` 的句柄（参见仓库 CLAUDE.md 对 GPUI 的说明）。UI 组件通常以 `Entity<InputField>` 的形式被父组件持有：

- `field.read(cx)` 拿到 `&InputField`（只读）；
- `field.update(cx, |field, cx| ...)` 拿到 `&mut InputField` 和 `&mut Context<InputField>`（可变更新）；
- `cx.new(|cx| ...)` 在实体池中创建新实体，闭包返回初始值。

所以「创建一个输入框」的完整惯用法是两步：`InputField::new(...)` 造出结构体，`cx.new(...)` 把它包成实体。你在上一讲已经见过这个形态，本讲会解释为什么必须这样分两步。

### 2.2 消费型 Builder 模式

Rust UI 代码里常见的链式配置写法：

```rust
InputField::new(window, cx, "占位文本")
    .label("API key")
    .tab_index(1)
    .masked(true)
```

每个配置方法签名都是 `fn xxx(mut self, ...) -> Self`——拿走所有权、改一个字段、再还回去。这样配置结束后所有权仍然唯一，可以直接交给 `cx.new`。它和「运行期修改」是两类不同的 API，本讲 4.3 节会对照讲。

### 2.3 类型擦除的内芯：ErasedEditor（承接 u1-l1）

上一讲已经建立的核心认知：`ui_input` 不能编译期依赖庞大的 `editor` crate，于是定义了 `ErasedEditor` trait 做类型擦除；`editor` crate 在 `init` 时把自己的构造函数注入全局工厂 `ERASED_EDITOR_FACTORY`。`InputField` 内芯的类型因此是 `Arc<dyn ErasedEditor>`，而不是 `Entity<Editor>`。本讲只需要记住这一点；trait 的逐方法分析与工厂注入的细节分别在 u3-l1、u3-l2 展开。

### 2.4 Window、App 与 Context

- `&mut App`：应用级上下文，能读全局状态、更新任意实体；
- `&mut Window`：窗口级上下文，焦点、绘制等需要它；
- `&mut Context<T>`：更新 `Entity<T>` 时的上下文，去引用后兼有 `App` 的能力，还多出 `cx.notify()`（请求重渲染）、`cx.listener(...)` 等。

注意 `InputField` 的很多方法只要 `&mut App` 而不是 `&mut Context<Self>`——这也是本讲要解释的一个设计点。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/ui_input.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/ui_input.rs) | crate 根：定义 `ErasedEditor` trait 与 `ERASED_EDITOR_FACTORY` | 工厂的类型签名、`set_placeholder_text` 等 trait 方法 |
| [src/input_field.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs) | `InputField` 组件本体 | 结构体字段、`new`、builder 方法、`Focusable` 与委托方法（本讲全部内容） |
| [../workspace/src/security_modal.rs](https://github.com/zed-industries-zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/security_modal.rs) | 消费者：工作区安全弹窗 | 创建、预填 `set_text`、读取 `text`、校验报错 `set_error` 的完整链路 |
| [../language_models/src/provider/llama_cpp.rs](https://github.com/zed-industries-zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/language_models/src/provider/llama_cpp.rs) | 消费者：llama.cpp 提供方配置页 | 多字段表单的 builder 链 + `set_text` 预填 |
| [../debugger_ui/src/new_process_modal.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/debugger_ui/src/new_process_modal.rs) | 消费者：调试器新进程弹窗 | `tab_index`/`tab_stop` 组合、`is_empty` 驱动按钮禁用、`clear` + `set_text` 回填 |
| [../component_preview/src/component_preview.rs](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs) | 消费者：组件预览面板 | `is_empty` + `text` 的组合读取（上一讲已见过该面板） |

## 4. 核心概念与源码讲解

### 4.1 InputField 结构体定义

#### 4.1.1 概念说明

`InputField` 采用「外壳 + 内芯」结构：

- **外壳**（本结构体自身）：只保存「配置」和「展示状态」——标签、图标、最小宽度、Tab 行为、错误文案等。它不保存用户输入的文本。
- **内芯**（`editor: Arc<dyn ErasedEditor>`）：一个被类型擦除的单行编辑器，真正持有文本、光标、选区、焦点。文本的每次读写最终都穿透外壳到达内芯。

这样拆分的直接收益是：外壳可以做得非常轻（一个普通 struct，字段全是配置项），而所有「编辑器才有的复杂度」（缓冲区、输入法、掩码渲染……）都被 `ErasedEditor` 这层 trait 边界挡在外面。

理解字段时有一个关键视角：**`Option` 字段往往是三态的，不是两态的**。例如 `masked: Option<bool>`：

| 取值 | 含义 |
| --- | --- |
| `None` | 这根本不是一个敏感字段，不渲染眼睛按钮 |
| `Some(true)` | 敏感字段，当前掩码显示，眼睛按钮可切换为明文 |
| `Some(false)` | 敏感字段，当前明文显示，眼睛按钮可切换为掩码 |

`tab_index: Option<isize>` 同理：`None` 表示「不干预 Tab 顺序」，`Some(i)` 表示「显式指定顺序」。这些三态语义在 4.4 节的焦点配置里会再次出现。

#### 4.1.2 核心流程

结构体字段按职责分三组：

```text
┌─ InputField ──────────────────────────────────────┐
│ 外观配置                                            │
│   label: Option<SharedString>        标签文案      │
│   label_size: LabelSize              标签字号      │
│   placeholder: SharedString          占位文本副本  │
│   start_icon: Option<IconName>       前置图标      │
│   min_width: Length                  输入框最小宽度│
│                                                     │
│ 交互配置                                            │
│   tab_index: Option<isize>           Tab 顺序      │
│   tab_stop: bool                     是否可 Tab 聚焦│
│   masked: Option<bool>               敏感字段三态  │
│                                                     │
│ 运行状态                                            │
│   editor: Arc<dyn ErasedEditor>      内芯（文本在这）│
│   error: Option<SharedString>        校验错误文案  │
└─────────────────────────────────────────────────────┘
```

#### 4.1.3 源码精读

结构体定义在 [src/input_field.rs:20-47](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L20-L47)，这段代码用文档注释逐字段说明了用途：

```rust
#[derive(RegisterComponent)]
pub struct InputField {
    /// An optional label for the text field.
    label: Option<SharedString>,
    /// The size of the label text.
    label_size: LabelSize,
    /// The placeholder text for the text field.
    placeholder: SharedString,

    editor: Arc<dyn ErasedEditor>,
    /// An optional icon that is displayed at the start of the text field.
    start_icon: Option<IconName>,
    /// The minimum width of for the input
    min_width: Length,
    /// The tab index for keyboard navigation order.
    tab_index: Option<isize>,
    /// Whether this field is a tab stop (can be focused via Tab key).
    tab_stop: bool,
    /// Whether the field content is masked (for sensitive fields like passwords or API keys).
    masked: Option<bool>,
    /// An optional validation error. When set, the field's border turns red
    /// and the message is shown as hint subtext below the field.
    error: Option<SharedString>,
}
```

逐字段说明（默认值来自 `new`，见 4.2.3）：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `label` | `Option<SharedString>` | `None` | 输入框上方的标签文案；`SharedString` 避免字符串拷贝 |
| `label_size` | `LabelSize` | `LabelSize::Small` | 标签字号，可选 `Default` 等（来自 `ui` crate） |
| `placeholder` | `SharedString` | 构造参数 | 占位文本的**副本**，注意它不是渲染占位符的数据源（见下方细节 1） |
| `editor` | `Arc<dyn ErasedEditor>` | 工厂产物 | 内芯编辑器，文本的真实存放地 |
| `start_icon` | `Option<IconName>` | `None` | 输入框前置图标，例如搜索框的放大镜 |
| `min_width` | `Length` | `px(192.).into()` | 输入框容器的最小宽度 |
| `tab_index` | `Option<isize>` | `None` | 显式 Tab 顺序；`None` 表示不干预 |
| `tab_stop` | `bool` | `true` | 是否允许 Tab 键聚焦 |
| `masked` | `Option<bool>` | `None` | 三态敏感字段标记（见表格上方） |
| `error` | `Option<SharedString>` | `None` | 校验错误文案，渲染时触发红边框 + 下方红色提示 |

`#[derive(RegisterComponent)]` 让这个类型可被组件预览系统登记（上一讲 u1-l2 已讲过 inventory 注册机制）。

**细节 1：`placeholder` 字段存储后并不用于渲染占位符。** 真正的占位文本在 `new` 里就已经通过 `set_placeholder_text` 推进了内芯编辑器（见 4.2.3 的 L61）；结构体里留存的这份副本，在 `render` 中唯一的用途是充当外层容器的元素 ID——[src/input_field.rs:176](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L176) 处的 `.id(self.placeholder.clone())`。也就是说它是「顺手复用占位文本当 ID」，不是占位符的数据源。

**细节 2：builder 名字和实际效果有偏差。** `label_min_width`（[src/input_field.rs:92-95](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L92-L95)）这个名字听起来像「标签的最小宽度」，但它写入的是 `min_width` 字段；而字段自己的文档注释写的是 "The minimum width of for the input"（输入框的最小宽度），`render` 中 `.min_w(self.min_width)` 也确实作用在输入框容器 `h_flex` 上（[src/input_field.rs:185](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L185)）。**读源码时要跟到字段的使用处，不要只信方法名。**

**细节 3：文档注释里有一处悬空链接。** [src/input_field.rs:24](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L24) 的注释提到 `[FieldLabelLayout]`，但用 `rg -n "FieldLabelLayout" crates` 全仓库检索只会命中这一行注释——这个类型在当前代码库中并不存在，是一处失效的 rustdoc 链接。遇到这类注释，以真实字段和 `render` 代码为准。

#### 4.1.4 代码实践

**实践目标**：用检索工具验证「字段的语义要跟到使用处才能确认」。

**操作步骤**：

1. 在 Zed 仓库根目录执行 `rg -n "min_width" crates/ui_input/src/input_field.rs`，确认 `min_width` 只在结构体定义、`label_min_width` builder、`new` 的默认值和 `render` 的 `.min_w(...)` 四处出现。
2. 再执行 `rg -n "self.placeholder" crates/ui_input/src/input_field.rs`，确认 `placeholder` 字段除了构造就只在 `.id(self.placeholder.clone())` 被读取。
3. 执行 `rg -n "FieldLabelLayout" crates`，亲眼确认悬空链接只有一处。

**需要观察的现象**：每条命令的命中行数都非常少（个位数），且都能对应到本节的分析。

**预期结果**：你会得到与细节 1、细节 2、细节 3 完全一致的结论。此实践为纯源码检索，无需构建，结果可直接确认。

#### 4.1.5 小练习与答案

**练习 1**：`masked` 为什么设计成 `Option<bool>` 而不是 `bool`？如果只是 `bool` 会丢失什么能力？

**参考答案**：`bool` 只能表达「掩码开 / 关」，无法表达「这不是敏感字段」。`Option<bool>` 的三态让 `None` 表示普通字段（渲染时不出现眼睛按钮），`Some(true)` / `Some(false)` 表示敏感字段的两种当前状态。`render` 里正是用 `.when_some(self.masked, ...)` 来决定是否渲染切换按钮（[src/input_field.rs:206](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L206)），`None` 时该分支整体跳过。

**练习 2**：`error` 字段为什么不像其他配置一样提供 builder 方法，而只有 `set_error`？

**参考答案**：错误是**运行期才发生的状态**（用户提交表单、校验失败时才产生），而 builder 方法是**构造期配置**。构造时通常还不知道有没有错误；即便知道，把「初始就有错误」编码进构造链也不符合使用习惯。所以它走 `set_error(&mut self, ..., cx)`，并且需要 `cx.notify()` 触发重渲染（详见 4.3.3）。

**练习 3**：`placeholder` 用 `SharedString` 而不是 `String`，好处是什么？

**参考答案**：`SharedString` 内部是 `&'static str` 或 `Arc<str>` 的枚举，克隆只是引用计数/指针拷贝，不复制字符串内容。`render` 里 `.id(self.placeholder.clone())` 每次渲染都会克隆一次，用 `String` 会带来无谓的堆分配。

### 4.2 InputField::new 与工厂调用

#### 4.2.1 概念说明

`new` 是组件的出生点，它要回答三个问题：

1. **内芯从哪来？** 不能直接 `Editor::...` 构造（会引入对 `editor` crate 的编译期依赖），所以从全局工厂 `ERASED_EDITOR_FACTORY` 取构造函数。
2. **占位文本给谁？** 占位符是编辑器行为（空内容时显示灰字），属于内芯的职责，所以立刻调用 trait 方法 `set_placeholder_text` 推进去。
3. **默认配置是什么？** 结构体所有字段在这里给出初始值，之后由 builder 链覆盖。

#### 4.2.2 核心流程

```text
InputField::new(window, cx, "placeholder 文本")
  │
  ├─ ① ERASED_EDITOR_FACTORY.get()
  │      从 OnceLock 里取出函数指针；
  │      若为 None（editor::init 未执行过）→ expect 直接 panic
  │
  ├─ ② (editor_factory)(window, cx)
  │      调用工厂函数，得到 Arc<dyn ErasedEditor]
  │      （editor crate 注入的实现会创建一个单行 Editor 实体并包装）
  │
  ├─ ③ editor.set_placeholder_text(placeholder_text, window, cx)
  │      占位文本立即推给内芯——此后外壳只留一份副本当元素 ID
  │
  └─ ④ Self { label: None, label_size: Small, ..., error: None }
         用全默认值组装外壳，等待 builder 链覆盖
```

#### 4.2.3 源码精读

构造函数在 [src/input_field.rs:56-75](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L56-L75)：

```rust
pub fn new(window: &mut Window, cx: &mut App, placeholder_text: &str) -> Self {
    let editor_factory = crate::ERASED_EDITOR_FACTORY
        .get()
        .expect("ErasedEditorFactory to be initialized");
    let editor = (editor_factory)(window, cx);
    editor.set_placeholder_text(placeholder_text, window, cx);

    Self {
        label: None,
        label_size: LabelSize::Small,
        placeholder: SharedString::new(placeholder_text),
        editor,
        start_icon: None,
        min_width: px(192.).into(),
        tab_index: None,
        tab_stop: true,
        masked: None,
        error: None,
    }
}
```

这段代码做了四件事：

1. **取工厂并断言已初始化**（L57-59）：`ERASED_EDITOR_FACTORY` 定义在 crate 根 [src/ui_input.rs:44-45](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/ui_input.rs#L44-L45)，类型是 `OnceLock<fn(&mut Window, &mut App) -> Arc<dyn ErasedEditor>>`。`.get()` 返回 `Option`，`.expect("ErasedEditorFactory to be initialized")` 把「工厂必须已注入」定为硬前提——违反就 panic 而非报错恢复。正常应用启动时 `editor::init` 会先执行；但**在测试或独立小工具里忘了初始化，这里就是崩溃点**（该前提的完整分析在 u3-l2）。
2. **调用工厂造内芯**（L60）：`(editor_factory)(window, cx)` 就是一次普通函数调用，拿到 `Arc<dyn ErasedEditor>`。
3. **立刻设置占位文本**（L61）：调用的是 trait 方法 [src/ui_input.rs:20](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/ui_input.rs#L20) 的 `set_placeholder_text`。注意它需要 `window` 和 `cx`，因为内芯是活着的实体，修改它属于「更新实体」而不只是改一个字段。
4. **默认值组装**（L63-74）：注意 `min_width` 默认 `px(192.).into()`（192 逻辑像素），`tab_stop` 默认 `true`，`label_size` 默认 `Small`——预览面板里 Small Label 示例不加 `.label_size(...)` 就是小字号的原因。

签名值得注意：`new` 只要 `&mut App`，**不要** `&mut Context<Self>`。因为创建发生在实体存在之前（`cx.new` 的闭包里 `Self` 还没诞生），此时能给的就是应用级上下文。这也解释了 2.1 节的两步惯用法：`cx.new(|cx| InputField::new(window, cx, "..."))` 中闭包参数 `cx` 是 `&mut Context<InputField>`，它会自动去引用成 `&mut App` 传给 `new`。

#### 4.2.4 代码实践

**实践目标**：跟踪一次真实的创建调用链，确认 `new` 总是发生在 `cx.new` 闭包里。

**操作步骤**：

1. 打开 [../workspace/src/security_modal.rs:315](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/security_modal.rs#L315)，观察 `let trust_path_input = cx.new(|cx| InputField::new(window, cx, "Folder to trust"));`。
2. 打开 [../language_models/src/provider/llama_cpp.rs:1183-1198](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/language_models/src/provider/llama_cpp.rs#L1183-L1198)，观察三个字段的创建，全部是同一形态。
3. 执行 `rg -n "InputField::new\(" crates --glob '*.rs' | grep -v tutorial`，浏览所有调用点。

**需要观察的现象**：所有调用点无一例外都是 `cx.new(|cx| InputField::new(window, cx, "…")…)` 的形态（个别地方变量名叫 `_window`，说明该处暂时没用到窗口上下文）。

**预期结果**：确认「`cx.new` 包裹 + `new` 只要 `&mut App`」是全仓库统一惯用法。此实践为源码阅读型，无需构建，可直接确认。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `editor.set_placeholder_text(...)` 这一行删掉，占位符还能显示吗？

**参考答案**：不能。渲染占位符是内芯编辑器的行为，外壳的 `placeholder` 字段只用来当元素 ID（4.1.3 细节 1）。删掉这一行后内芯永远不知道占位文本是什么，输入框空着时不会有灰字提示。

**练习 2**：`new` 的第三个参数是 `&str`，但字段存的是 `SharedString::new(placeholder_text)`，同时 trait 方法又收到一份 `placeholder_text`。这个字符串被「用」了几次？

**参考答案**：两次有效使用：一次推进内芯（L61，成为真正的占位符），一次存进外壳字段（L66，成为元素 ID 的素材）。`SharedString::new(&str)` 会发生一次分配，这是很小的、一次性的构造期开销。

**练习 3**：为什么 `InputField::new` 不做成 `InputField::new(cx)` 然后 builder 里再传 `window`？

**参考答案**：`new` 必须立刻调用工厂和 `set_placeholder_text`，这两者都需要 `window` 和 `cx`（更新内芯实体是涉及窗口焦点的操作）。参数只能当场给全，之后 builder 方法才可以只操作纯数据字段、不再要上下文——这正是「构造期要上下文、配置期不要上下文」的分工。

### 4.3 builder 链式方法

#### 4.3.1 概念说明

builder 方法解决的问题是：**结构体有 10 个字段，但常见用法只想设置其中两三个**。如果用带 10 个参数的构造函数，每个调用点都要写一堆 `None`；builder 链则让调用点只写关心的配置，其余保持 `new` 里的默认值。

Rust 里这类 API 的标准形态是「消费型 builder」：`fn label(mut self, ...) -> Self`。每个方法拿走 `self` 的所有权，修改后归还，于是可以无限链下去，链的终点就是配置完成的值。

#### 4.3.2 核心流程

```text
InputField::new(window, cx, "sk-...")   ← 全默认外壳 + 已就位的内芯
    .label("API key")                    ← label = Some("API key")
    .start_icon(IconName::Key)           ← start_icon = Some(Key)
    .masked(true)                        ← masked = Some(true)
                                          ↓ 拿到配置完成的 InputField
cx.new(|_| …)                             ← 交给实体系统
```

注意三类 API 的分工：

| 类别 | 形态 | 时机 | 例子 |
| --- | --- | --- | --- |
| 构造期配置 | `mut self -> Self`（builder） | 创建时 | `label` / `masked` / `tab_index` … |
| 运行期状态 | `&mut self, cx: &mut Context<Self>` | 实体存活期间 | `set_error` |
| 运行期读写 | `&self, window, cx: &mut App` | 实体存活期间 | `text` / `set_text` / `clear`（见 4.4） |

#### 4.3.3 源码精读

七个 builder 方法集中在 [src/input_field.rs:77-111](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L77-L111)，每个都是同一模式（拿走 `mut self`、写一个字段、归还 `self`）：

```rust
pub fn start_icon(mut self, icon: IconName) -> Self {
    self.start_icon = Some(icon);
    self
}

pub fn label(mut self, label: impl Into<SharedString>) -> Self {
    self.label = Some(label.into());
    self
}

pub fn label_size(mut self, size: LabelSize) -> Self { … }
pub fn label_min_width(mut self, width: impl Into<Length>) -> Self { … }
pub fn tab_index(mut self, index: isize) -> Self { … }
pub fn tab_stop(mut self, tab_stop: bool) -> Self { … }

/// Sets this field as a masked/sensitive input (e.g., for passwords or API keys).
pub fn masked(mut self, masked: bool) -> Self {
    self.masked = Some(masked);
    self
}
```

几个值得留意的签名细节：

- `label(mut self, label: impl Into<SharedString>)`：接收 `impl Into<SharedString>`，所以调用点既能传 `&str` 也能传 `String` / `SharedString`，这是 Rust API 的常见礼貌。
- `label_min_width(mut self, width: impl Into<Length>)`：同样宽进；但记住 4.1.3 细节 2——它实际设置的是**输入框**的 `min_width`。
- `tab_index(mut self, index: isize)`：写入 `Some(index)`，与 `tab_stop` 组合出三种焦点行为（完整分析在 u2-l3，本讲先知道字段写什么）。

与之对照，运行期的 `set_error` 在 [src/input_field.rs:113-119](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L113-L119)：

```rust
/// Sets a validation error message, turning the field's border red and
/// showing the message as hint subtext below the field. Pass `None` to
/// clear the error.
pub fn set_error(&mut self, error: Option<impl Into<SharedString>>, cx: &mut Context<Self>) {
    self.error = error.map(Into::into);
    cx.notify();
}
```

两个差异：它借 `&mut self` 而不是消费 `self`（实体已经存在，只能借用修改）；它要 `&mut Context<Self>` 并调用 `cx.notify()`——错误文案改变会影响渲染，必须请求 GPUI 重新渲染这个视图。builder 方法不需要 `cx`，因为构造期还没人渲染它。

真实调用点是最好的教材。[../debugger_ui/src/new_process_modal.rs:830-842](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/debugger_ui/src/new_process_modal.rs#L830-L842) 用链式配置了两个带 Tab 顺序的表单字段：

```rust
let program = cx.new(|cx| {
    InputField::new(window, cx, "ENV=Zed ~/bin/program --option")
        .label("Program")
        .tab_stop(true)
        .tab_index(1)
});

let cwd = cx.new(|cx| {
    InputField::new(window, cx, "Ex: $ZED_WORKTREE_ROOT")
        .label("Working Directory")
        .tab_stop(true)
        .tab_index(2)
});
```

而 [../language_models/src/provider/llama_cpp.rs:1183-1198](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/language_models/src/provider/llama_cpp.rs#L1183-L1198) 展示了「builder 链 + 立刻 `set_text` 预填」的组合（预填细节见 4.4.3）：

```rust
let api_key_editor = cx.new(|cx| InputField::new(window, cx, "sk-...").label("API key"));

let api_url_editor = cx.new(|cx| {
    let input = InputField::new(window, cx, LLAMA_CPP_API_URL).label("API URL");
    input.set_text(&LlamaCppLanguageModelProvider::api_url(cx), window, cx);
    input
});
```

#### 4.3.4 代码实践

**实践目标**：体会「同一需求」在 builder 链和运行期方法两种写法中的差异。

**操作步骤**：

1. 打开上面两个真实调用点，数一数每个 `cx.new` 闭包里有几层方法调用。
2. 阅读预览实现 [src/input_field.rs:248-271](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L248-L271)：两个示例分别用 `.label("Small Label")` 和 `.label("Regular Label").label_size(LabelSize::Default)` 区分变体。
3. 心算回答：如果要加一个「带放大镜图标的搜索框」示例，链式调用应该怎么写？（答案见练习 1）

**需要观察的现象**：builder 方法只写关心的字段，其余保持默认；两个示例只差一行 `.label_size(...)`。

**预期结果**：确认 builder 链的每个调用点都只出现 0 到 3 个配置方法，没有一处把全部字段列全。此实践为源码阅读型，可直接确认。

#### 4.3.5 小练习与答案

**练习 1**：写出一个「搜索框」的创建表达式：占位文本 `Search…`、标签 `Search`、前置放大镜图标、宽度至少 300 逻辑像素。

**参考答案**（示例代码，仿照真实调用点风格）：

```rust
let search = cx.new(|cx| {
    InputField::new(window, cx, "Search…")
        .label("Search")
        .start_icon(IconName::MagnifyingGlass)
        .label_min_width(px(300.))
});
```

注意两点：图标名要查 `ui` crate 的 `IconName` 枚举确认拼写（`MagnifyingGlass` 是否存在以 `rg -n "MagnifyingGlass" crates/ui/src` 为准，待确认）；宽度用 `label_min_width`（虽然名字有误导，见 4.1.3 细节 2）。

**练习 2**：`.masked(false)` 和不调用 `.masked(...)` 效果一样吗？

**参考答案**：不一样。不调用时 `masked == None`，不是敏感字段，渲染时没有眼睛按钮；`.masked(false)` 时 `masked == Some(false)`，是敏感字段但当前明文显示，**有**眼睛按钮且初始状态为明文。布尔值控制初始显示状态，`Option` 控制是否属于敏感字段。

**练习 3**：为什么 `set_error` 不能也做成 `mut self -> Self` 的 builder？

**参考答案**：builder 消费 `self`，适合「构造前配置」；而错误在实体存活期间随时可能设置或清除（例如用户反复提交表单）。此时手上只有 `Entity<InputField>`，做法是 `entity.update(cx, |field, cx| field.set_error(Some("…"), cx))`——借 `&mut self` 修改，无法消费 `self`。此外它还需要 `cx.notify()` 请求重渲染，builder 方法没有 `cx` 可用。

### 4.4 Focusable 委托与文本读写委托方法

#### 4.4.1 概念说明

外壳的一大职责是**委托**（delegation）：把外部世界对 `InputField` 的请求，原样转交给内芯。本模块覆盖两组委托：

1. **焦点委托**：GPUI 的 `Focusable` trait 要求提供 `focus_handle`。`InputField` 不自己管理焦点，直接返回内芯的句柄。这样键盘事件分发、keymap 上下文匹配、`window.focus(...)` 等机制看到的都是「真正的焦点持有者」——那个活着的编辑器实体。
2. **文本读写委托**：`text` / `set_text` / `clear` / `set_masked` / `is_empty` / `editor` 访问器。消费者永远不直接碰 `Arc<dyn ErasedEditor>` 的构造细节，都从外壳走。

一个初看反直觉的点：**这些修改文本的方法只接收 `&self`**。`set_text(&self, ...)` 明明改了内容，为什么不可变借用就够？因为可变状态不在外壳里——它在内芯背后的编辑器**实体**中。实体的更新走 `entity.update(cx, ...)`，由 GPUI 的内部可变性保证安全；外壳手里的 `Arc<dyn ErasedEditor>` 只是个共享句柄。这正是「外壳轻、内芯重」拆分的红利。

#### 4.4.2 核心流程

```text
消费者代码
   │ field.read(cx).text(cx)
   ▼
InputField::text(&self, cx)            外壳委托
   │ self.editor().text(cx)
   ▼
dyn ErasedEditor::text(&self, cx)      trait 动态分发
   │ （editor crate 中的实现）
   ▼
Entity<Editor>.read(cx) → buffer 内容拷贝为 String   真实数据

焦点路径：
InputField::focus_handle(&self, cx)
   │ self.editor.focus_handle(cx)
   ▼
内芯编辑器实体的 FocusHandle              焦点真正挂在这里
```

#### 4.4.3 源码精读

**焦点委托**在 [src/input_field.rs:49-53](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L49-L53)：

```rust
impl Focusable for InputField {
    fn focus_handle(&self, cx: &App) -> FocusHandle {
        self.editor.focus_handle(cx)
    }
}
```

`Focusable` 来自 `gpui`（见文件头 [src/input_field.rs:3](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L3) 的导入）。这三行的含义：凡是问「这个 InputField 的焦点句柄是什么」，答案永远是内芯编辑器的句柄。渲染时的 `.track_focus(&configured_handle)`（[src/input_field.rs:184](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L184)）挂的也是这个句柄（外加 Tab 配置包装，那属于 u2-l3 的主题）。

**文本读写委托**集中在 [src/input_field.rs:121-143](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L121-L143)：

```rust
pub fn is_empty(&self, cx: &App) -> bool {
    self.editor().text(cx).trim().is_empty()
}

pub fn editor(&self) -> &Arc<dyn ErasedEditor> {
    &self.editor
}

pub fn text(&self, cx: &App) -> String {
    self.editor().text(cx)
}

pub fn clear(&self, window: &mut Window, cx: &mut App) {
    self.editor().clear(window, cx)
}

pub fn set_text(&self, text: &str, window: &mut Window, cx: &mut App) {
    self.editor().set_text(text, window, cx)
}

pub fn set_masked(&self, masked: bool, window: &mut Window, cx: &mut App) {
    self.editor().set_masked(masked, window, cx)
}
```

逐个说明：

- `is_empty(&self, cx: &App)`：注意它先取全文再 `trim()`——只包含空白的输入也视为空。这是表单校验「必填项」的常见语义。它只要只读的 `&App`。
- `editor(&self)`：内芯的公开访问器，返回 `&Arc<dyn ErasedEditor>`。消费者可以用它调用 trait 上更冷门的方法（如 `set_read_only`、`subscribe`），见下方 security_modal 的实例。
- `text(&self, cx: &App) -> String`：读取是**拷贝**——trait 方法签名（[src/ui_input.rs:17](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/ui_input.rs#L17)）返回 `String`。类型擦除的代价之一，u4-l3 会展开。
- `clear` / `set_text` / `set_masked(&self, ..., window, cx: &mut App)`：修改内芯实体，所以要 `window` 和可变的 `App`；但外壳本身只需 `&self`——理由见 4.4.1。
- `set_masked` 与 builder 的 `.masked(bool)` 互补：builder 决定「是不是敏感字段、初始是否掩码」，`set_masked` 在运行期切换掩码状态（眼睛按钮的点击处理调用的就是它，[src/input_field.rs:219-227](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L219-L227)）。

**消费者解剖一：security_modal（读 + 预填 + 报错）**。这是本讲规格里指定的范例。[../workspace/src/security_modal.rs:40](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/security_modal.rs#L40) 声明字段 `trust_path_input: Entity<InputField>`；创建后立刻做了三件事（[../workspace/src/security_modal.rs:329-338](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/security_modal.rs#L329-L338)）：

```rust
// Pre-fill with the single project's parent folder (today's static
// scope), read-only until the checkbox is ticked.
if let Some(project) = this.single_trustable_path() {
    let default_scope = project.parent().unwrap_or(&project).to_path_buf();
    this.trust_path_input.update(cx, |field, cx| {
        field.set_text(&default_scope.to_string_lossy(), window, cx);
    });
}
let editor = this.trust_path_input.read(cx).editor().clone();
editor.set_read_only(!this.trust_parents, cx);
```

- 预填用 `entity.update` + `set_text`（`set_text` 是 `&self` 方法，但在 `update` 闭包里拿到的本来就是一个引用，直接调用）；
- `read(cx).editor().clone()` 克隆一个 `Arc`（引用计数 +1，代价极小），然后调用 trait 的 `set_read_only`——外壳没封装的方法就经 `editor()` 逃生舱走 trait。

读取与校验在提交时发生（[../workspace/src/security_modal.rs:391](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/security_modal.rs#L391) 与 [../workspace/src/security_modal.rs:401-407](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/security_modal.rs#L401-L407)）：

```rust
let typed = self.trust_path_input.read(cx).text(cx);      // 读
…
// Invalid path: flag the field and keep the modal open.
self.trust_path_input
    .update(cx, |input, cx| input.set_error(Some(error), cx));   // 报错
```

**消费者解剖二：new_process_modal（is_empty 驱动交互）**。[../debugger_ui/src/new_process_modal.rs:852-858](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/debugger_ui/src/new_process_modal.rs#L852-L858) 是「空则回填」的典型：

```rust
fn load(&mut self, cwd: PathBuf, window: &mut Window, cx: &mut App) {
    self.cwd.update(cx, |input_field, cx| {
        if input_field.is_empty(cx) {
            input_field.set_text(&cwd.to_string_lossy(), window, cx);
        }
    });
}
```

同一个文件里，`is_empty` 还被用来禁用按钮（[../debugger_ui/src/new_process_modal.rs:741-749](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/debugger_ui/src/new_process_modal.rs#L741-L749)）：`.disabled(self.debugger.is_none() || self.configure_mode.read(cx).program.read(cx).is_empty(cx))`；`text` 用于提交时取值（[../debugger_ui/src/new_process_modal.rs:860-866](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/debugger_ui/src/new_process_modal.rs#L860-L866)）；`clear` + `set_text` 成对出现做整体重置（[../debugger_ui/src/new_process_modal.rs:1639-1648](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/debugger_ui/src/new_process_modal.rs#L1639-L1648)）。组件预览面板的过滤框也是同样的组合（[../component_preview/src/component_preview.rs:577-583](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/component_preview/src/component_preview.rs#L577-L583)）：

```rust
let current_filter = self.filter_editor.update(cx, |input, cx| {
    if input.is_empty(cx) {
        String::new()
    } else {
        input.text(cx)
    }
});
```

#### 4.4.4 代码实践

**实践目标**：把「焦点委托 + 读写委托」串成一条可跟踪的调用链。

**操作步骤**：

1. 从 [src/input_field.rs:50-52](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L50-L52) 的 `focus_handle` 出发，打开 crate 根的 trait 定义 [src/ui_input.rs:27](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/ui_input.rs#L27)，确认 trait 里也有一个 `focus_handle`——外壳的实现就是把两者接起来。
2. 在 security_modal 里跟踪 `set_read_only`：外壳没有这个方法 → 走 `editor()` 访问器 → trait 的 [src/ui_input.rs:24](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/ui_input.rs#L24)。记下这条「逃生舱」路径。
3. 用 `rg -n "set_read_only" crates/editor/src/editor.rs` 找到 editor crate 侧 `ErasedEditorImpl` 对该方法的实现位置（u3-l1 会正式精读，这里只看它存在）。

**需要观察的现象**：`InputField` 的焦点、文本能力全部能沿「外壳 → trait → editor 实现」三跳追下去，中途没有任何魔法。

**预期结果**：得到一张三行的小表：`focus_handle` / `text` / `set_read_only` 各自的外壳入口、trait 声明位置、editor 侧实现位置（第三列的行号待你本地检索确认）。此实践为源码跟踪型，前两跳可直接确认。

#### 4.4.5 小练习与答案

**练习 1**：`set_text` 是 `&self` 方法，那 `entity.update(cx, |field, cx| field.set_text(...))` 里的 `update` 是不是多余的？能不能 `entity.read(cx).set_text(...)`？

**参考答案**：不能。`set_text` 需要 `&mut App`（和 `&mut Window`）去更新内芯背后的编辑器实体；`read(cx)` 只借用 `&App`，编译都过不了。`update` 闭包提供的 `cx: &mut Context<InputField>` 去引用后兼有 `&mut App` 的能力，才能满足签名。`&self` 只说明**外壳**不需要可变借用，不等于整个调用链不需要可变上下文。

**练习 2**：`is_empty` 为什么要 `trim()`？举个会因此产生不同结果的输入。

**参考答案**：不 trim 的话，只输入空格的字段会被当作「已填写」，必填校验形同虚设。输入 `"  "`（两个空格）时：trim 后 `is_empty` 返回 `true`（视为空）；若实现是 `text(cx).is_empty()` 则返回 `false`。[src/input_field.rs:121-123](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L121-L123) 选择了前者。

**练习 3**：为什么 `editor()` 访问器要公开？把它藏起来、只在外壳上补齐所有方法不是更封装吗？

**参考答案**：那样 `InputField` 的方法集合会无限膨胀——`ErasedEditor` 有 13 个方法（文本 9、焦点 1、订阅 1、渲染与还原 2），还要随 trait 演进同步。公开 `editor()` 让消费者按需走 trait（security_modal 的 `set_read_only`、skill_creator 的 `subscribe` 都是这么用的），外壳只封装最高频的几个。这是「便利性封装 + 逃生舱」的折中，代价是消费者与 trait 耦合。

## 5. 综合实践

本讲的贯穿任务：**仿照真实消费者，亲手写一段「创建 + 读取 + 回填」的完整代码**。

### 5.1 任务说明

参照 [../debugger_ui/src/new_process_modal.rs:828-858](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/debugger_ui/src/new_process_modal.rs#L828-L858)（`ConfigureMode` 的两个字段及其 `load` 方法）和 [../workspace/src/security_modal.rs:329-338](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/workspace/src/security_modal.rs#L329-L338)（预填模式），写一个最小表单结构体。

### 5.2 参考实现（示例代码，非项目原有代码）

```rust
use gpui::{App, Entity, Window};
use ui::IconName;
use ui_input::InputField;

/// 一个最小表单：收集服务地址与 API 密钥。
struct EndpointForm {
    url: Entity<InputField>,
    api_key: Entity<InputField>,
}

impl EndpointForm {
    fn new(window: &mut Window, cx: &mut App) -> Self {
        // builder 链：label + 前置图标 + 敏感字段
        let url = cx.new(|cx| {
            InputField::new(window, cx, "https://example.com")
                .label("Endpoint URL")
                .start_icon(IconName::Server)
        });
        let api_key = cx.new(|cx| {
            InputField::new(window, cx, "sk-…")
                .label("API Key")
                .masked(true) // 敏感字段：渲染眼睛按钮，初始掩码
        });
        Self { url, api_key }
    }

    /// 读取：两个字段都非空才允许提交（trim 语义由 is_empty 提供）。
    fn is_complete(&self, cx: &App) -> bool {
        !self.url.read(cx).is_empty(cx) && !self.api_key.read(cx).is_empty(cx)
    }

    /// 回填：只在字段为空时预填，尊重用户已有输入（模仿 new_process_modal::load）。
    fn prefill_if_empty(&self, default_url: &str, window: &mut Window, cx: &mut App) {
        self.url.update(cx, |field, cx| {
            if field.is_empty(cx) {
                field.set_text(default_url, window, cx);
            }
        });
    }
}
```

写完后逐行自查三个问题：

1. `new` 里每个字段是否都遵循 `cx.new(|cx| InputField::new(window, cx, …).builder链…)` 的两步惯用法？
2. `is_complete` 里用的 `&App` 是否与 `is_empty(&self, cx: &App)` 的签名匹配？
3. `prefill_if_empty` 里 `update` 闭包提供的 `cx` 为什么够 `set_text` 用？（答案在 4.4.5 练习 1）

### 5.3 验证方式

- **静态验证**：把这段代码（连同必要的 `use`）临时放进一个已有消费者 crate 的测试模块或独立文件，运行 `cargo check -p <那个 crate>` 看类型是否吻合。图标名 `IconName::Server` 是否存在需先 `rg -n "Server" crates/ui/src/icon.rs` 确认，不存在就换一个真实枚举值（待本地验证）。
- **动态验证（可选）**：把 `EndpointForm::new` 中某个字段临时加进 [src/input_field.rs:248-271](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L248-L271) 的 `preview`（作为第三个 `single_example`），重新构建后打开 `workspace: open component preview` 查看 `.masked(true)` 的眼睛按钮与前置图标效果。此路径需要完整构建 Zed，效果待本地验证；注意这是本地练习改动，验证后应还原，不要提交。
- 无论哪种方式，都不要把示例代码提交进仓库——它是练习，不是功能。

## 6. 本讲小结

- `InputField` 是「外壳 + 内芯」结构：外壳 9 个字段全是配置与展示状态，文本等可变状态全在内芯 `editor: Arc<dyn ErasedEditor>` 里；`Option` 字段（`masked`、`tab_index`）是三态的，`None` 往往表示「不启用该行为」。
- `new` 的四步：从 `ERASED_EDITOR_FACTORY` 取工厂（未初始化则 panic）→ 调工厂造内芯 → 立刻把占位文本推进内芯 → 用全默认值组装外壳；签名只要 `&mut App`，因为实体尚未诞生。
- builder 方法（`label` / `label_size` / `label_min_width` / `start_icon` / `tab_index` / `tab_stop` / `masked`）统一是 `mut self -> Self` 的构造期配置；运行期状态走 `set_error(&mut self, …, cx)` 并需要 `cx.notify()`。注意 `label_min_width` 实际设置的是输入框的 `min_width`，名字有误导。
- `Focusable::focus_handle` 直接返回内芯的焦点句柄，键盘事件与 keymap 因此落在真正的编辑器上；`text` / `set_text` / `clear` / `set_masked` / `is_empty` / `editor()` 是外壳到内芯的委托，修改文本只需 `&self`，因为可变性在内芯实体里。
- 真实消费者的三种惯用形态：security_modal 的「预填 + `editor()` 逃生舱 + `set_error` 报错」、new_process_modal 的「`is_empty` 判空后 `set_text` 回填 / 驱动按钮禁用」、llama_cpp 配置页的「多字段 builder 链 + `set_text` 预填」。

## 7. 下一步学习建议

本讲搞定了「数据长什么样、API 怎么用」，但这些字段最终如何变成屏幕上的像素还没有展开。下一讲 **u2-l2《render 方法拆解：布局、主题与条件样式》**将逐段阅读 [src/input_field.rs:146-235](https://github.com/zed-industries/zed/blob/91bf967e279fba3b326c096aeb66053cb2373547/crates/ui_input/src/input_field.rs#L146-L235)：`v_flex` / `h_flex` 两层布局、`InputFieldStyle` 如何从主题取色、`.when` / `.when_some` 条件渲染，以及 `start_icon` 与 `editor.render(window, cx)` 如何作为 child 嵌入。

在进入下一讲之前，建议先自己通读一遍 `render` 方法并试着回答：本讲学过的每个字段（`label`、`min_width`、`masked`、`error`……）分别出现在 `render` 的哪一行？带着这张「字段 → 渲染」对照表去读 u2-l2 会顺畅得多。之后 u2-l3 会补齐焦点跟踪、Tab 配置与 masked 切换按钮的交互细节，u3-l1 / u3-l2 再回到 `ErasedEditor` trait 本身与工厂注入机制。
