# 键位派发链路：Keymap、KeyContext 与 DispatchTree

## 1. 本讲目标

上一讲（u5-l3）我们知道了「动作是什么、怎么定义、怎么注册 `on_action` 监听器」，但留下了一个关键问题：**按下键盘上的一个键，GPUI 怎么知道该派发哪个动作、派发给谁？**

本讲学完后，你应该能够：

1. 追踪一次按键的完整旅程：平台 `Keystroke` → `Keymap` 匹配 `KeyBinding` → `DispatchTree` 沿元素树与 `KeyContext` 栈寻找 `on_action` 处理器。
2. 解释 `KeyContext` 栈如何参与绑定匹配的优先级计算（深度优先、同深度后加优先、来源分层）。
3. 理解 `DispatchTree` 的构建时机（绘制阶段）与复用优化（`reuse_subtree`）。
4. 掌握一套系统的「按键没反应」排查路径。

## 2. 前置知识

本讲假设你已经理解以下概念（前面讲义已覆盖）：

- **Action（动作）**：命名的、类型安全的用户意图，如 `actions!(counter, [Increment, Decrement])` 定义的单元结构体动作。动作通过 `TypeId` 被识别（见 u5-l3）。
- **`on_action` 监听器**：在元素的 **paint 阶段**注册，写入下一帧的派发树，只活一帧（见 u5-l2、u5-l3）。
- **焦点（Focus）**：每个可聚焦元素有一个 `FocusHandle`，键盘事件的派发路径由**焦点位置**决定，而不是鼠标位置（见 u5-l1）。
- **三阶段绘制**：`request_layout` → `prepaint` → `paint`（见 u4-l1）。本讲会精确到「哪个阶段往派发树里写什么」。
- **元素树每帧重建**：声明式 UI 是立即模式的，所以「监听器注册在哪、活多久」都和帧周期绑定（见 u3-l1、u4-l3）。

两个本讲新引入的基础术语：

- **Keymap（键位表）**：一个存放 `KeyBinding`（按键 → 动作的映射规则）的容器，独立于任何窗口存在，挂在 `App` 上。
- **DispatchTree（派发树）**：每帧随元素树一起重建的一棵平行树，记录「哪个节点注册了哪些监听器、声明了什么上下文」，按键匹配在它上面完成。

## 3. 本讲源码地图

| 文件 | 职责 |
| --- | --- |
| [src/platform/keystroke.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform/keystroke.rs) | `Keystroke` 结构、按键字符串的解析（`"ctrl-a"`）与跨键盘布局匹配（`should_match`） |
| [src/keymap/binding.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/binding.rs) | `KeyBinding`：一条绑定 = 按键序列 + 动作 + 可选上下文谓词 + 来源元数据 |
| [src/keymap/context.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs) | `KeyContext`（上下文事实）与 `KeyBindingContextPredicate`（上下文谓词小语言） |
| [src/keymap.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs) | `Keymap`：绑定的存储、按动作反查索引，以及核心的优先级匹配算法 `bindings_for_input` |
| [src/key_dispatch.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs) | `DispatchTree`/`DispatchNode`：监听器树、`dispatch_path`、多键序列状态机 `dispatch_key` |
| [src/window.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs) | 事件入口 `dispatch_key_event`、四阶段动作派发 `dispatch_action_on_node_inner`、pending 输入与超时重放 |
| [src/element.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/element.rs) | 元素 prepaint/paint 时对派发树节点的 push/pop |
| [src/elements/div.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs) | `key_context()` 链式方法与 `paint_keyboard_listeners`（把交互配置写进派发树） |
| [src/app.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs) | `App` 上的 `keymap` 字段与 `bind_keys` 入口 |
| [docs/key_dispatch.md](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/docs/key_dispatch.md) | 官方说明：动作定义、`key_context` 声明与 JSON 键位表格式 |
| [examples/testing.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/testing.rs) | 综合实践的参照示例：`key_context("Counter")` + `bind_keys` + 键盘测试 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块，按一次按键流动的方向排列：

```text
平台按键事件
   │
   ▼
4.1 Keystroke ──按键的统一表示与匹配
   │
   ▼
4.2 KeyBinding + KeyContext ──一条绑定规则与它的上下文谓词
   │
   ▼
4.3 Keymap ──所有绑定 + 优先级算法
   │
   ▼
4.4 DispatchTree ──每帧重建的监听器树（匹配发生在哪里）
   │
   ▼
4.5 window.rs 派发链路 ──从 dispatch_key_event 到四阶段动作派发
```

### 4.1 Keystroke：按键的统一表示与匹配

#### 4.1.1 概念说明

`Keystroke` 是「一次按键」的框架内统一表示。u5-l1 已经介绍过它的三元组结构（`modifiers` / `key` / `key_char`），本讲关心的是它的另外两个职责：

1. **解析**：把 `"ctrl-shift-a"` 这样的字符串变成结构化的 `Keystroke`——这是 `KeyBinding::new` 的第一个参数的来历。
2. **匹配**：用户实际敲下的键（可能带有键盘布局差异）是否命中键位表里的目标键——这是键位匹配的最底层原语。

框架内部把「键位表里的键」包成了 `KeybindingKeystroke`（除了 `Keystroke` 本体，Windows 上还带展示用的修饰键/键名），这样「用户敲的」和「表里写的」是两种类型，匹配函数签名一目了然。

#### 4.1.2 核心流程

`Keystroke::parse` 的语法（文档注释原文）是：

```text
[secondary-][ctrl-][alt-][shift-][cmd-][fn-]key[->key_char]
```

解析流程：

1. 按 `-` 切分输入串，逐段识别修饰键：`ctrl`、`alt`、`shift`、`fn`、`cmd`/`super`/`win`（平台键）、`secondary`（macOS 上等于 `cmd`，其他平台等于 `ctrl`）。
2. 剩下的一段是主键；单个 ASCII 大写字母会被规范化为 `shift + 小写`；命名键（`tab`、`enter`）统一转小写。
3. 特例：只写修饰键本身（如 `"shift"`）合法，此时该修饰键降级为主键。
4. `->key_char` 语法仅用于生成测试事件，匹配时忽略。

匹配 `should_match` 的关键在于**键盘布局差异**：捷克键盘上 `$` 要用 `alt-ç` 打出来，巴西键盘上 `"` 是 `" space` 两段输入。所以匹配时假定「self 是用户敲的，target 在键位表里」，同时检查两种可能——按 `key` 匹配或按 `key_char` 匹配（后者要求修饰键只剩下控制键和平台键，即 IME/布局注入的字符）。

#### 4.1.3 源码精读

`Keystroke` 结构与 `KeybindingKeystroke` 包装：[src/platform/keystroke.rs:17-46](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform/keystroke.rs#L17-L46)——`modifiers` 是按键瞬间的修饰键状态，`key` 是键帽印的字符，`key_char` 是「这次按键本可以输入的字符」。

解析入口 `Keystroke::parse`：[src/platform/keystroke.rs:115-219](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform/keystroke.rs#L115-L219)——逐段消费 `ctrl-`/`alt-`/… 前缀，末尾处理大写转 shift、`-` 结尾的连字符键（`"-"` 本身是合法键名）以及「只写修饰键」的回退。

跨布局匹配 `should_match`：[src/platform/keystroke.rs:73-113](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform/keystroke.rs#L73-L113)——`key_char` 与 `key` 不同时，先用 `key_char` 对照目标键尝试一次（要求修饰键只剩 control/platform），失败再回退到精确比较（`modifiers` 与 `key` 都相等）。Windows 分支略有不同：`key_char` 存在即视为「敲出的就是 key_char」。

`KeyBinding::match_keystrokes` 在此之上做**序列前缀匹配**：[src/keymap/binding.rs:89-101](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/binding.rs#L89-L101)——逐位用 `should_match` 比较；绑定的键比已输入的短则返回 `None`（不匹配）；全部吻合但绑定更长则返回 `Some(true)`（**pending**，多键序列进行中，如 `"cmd-k left"` 只敲了 `cmd-k`）；完全等长返回 `Some(false)`（命中）。这个三态返回值是整个多键序列机制的地基。

#### 4.1.4 代码实践

1. **实践目标**：直观掌握按键字符串的解析规则与三态匹配。
2. **操作步骤**：运行 `Keymap` 模块自带的单元测试（它们大量使用 `Keystroke::parse`）：
   ```bash
   cargo test -p gpui keymap::
   ```
   然后阅读 [src/keymap.rs:346-367](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L346-L367) 的 `test_depth_precedence`，注意它如何用 `Keystroke::parse("ctrl-a")` 构造输入。
3. **需要观察的现象**：测试全部通过；`test_depth_precedence` 里同一按键 `ctrl-a` 在 `pane` 与 `editor` 两个上下文中各有一条绑定，返回的两条结果有明确的先后顺序。
4. **预期结果**：`cargo test -p gpui keymap::` 输出 `ok`。测试里 `result[0]` 是 `ActionGamma`（editor 上下文），`result[1]` 是 `ActionBeta`（pane 上下文）——深层上下文排在前面，这正是 4.3 节要展开的优先级规则。
5. 「待本地验证」：如需观察 `parse` 的边界行为，可在测试中临时加断言（如 `Keystroke::parse("shift")` 的 `key` 应为 `"shift"` 且无修饰键），但本讲不修改源码，故标注为选做。

#### 4.1.5 小练习与答案

**练习 1**：`Keystroke::parse("secondary-p")` 在 Linux 上解析出什么？
**答案**：`secondary` 不是通用修饰键，而是平台别名——macOS 上设 `modifiers.platform = true`，其他平台（含 Linux）设 `modifiers.control = true`；主键为 `p`。见 [src/platform/keystroke.rs:143-150](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/platform/keystroke.rs#L143-L150)。这就是 Zed 键位表里 `secondary-` 前缀跨平台的原因。

**练习 2**：绑定 `"cmd-k left"`，用户刚敲完 `cmd-k`，`match_keystrokes` 返回什么？用户又敲了 `right` 呢？
**答案**：敲完 `cmd-k` 返回 `Some(true)`（pending：前缀吻合但绑定更长）；再敲 `right` 时第二位 `right` 对 `left` 匹配失败，返回 `None`。此时框架会走「重放」路径（见 4.5 节的 `replay_prefix`）。

**练习 3**：为什么 `should_match` 需要 `key_char` 这条额外分支？
**答案**：非美式键盘上常用符号藏在 `option`/`alt` 后面（文档注释举的例子：捷克键盘 `$` 是 `alt-ç`），IME 也可能把多段输入合成一个字符。只按 `key + modifiers` 精确匹配会让这些布局永远命中不了键位表；`key_char` 分支允许「敲出的字符」与目标键名对照（修饰键只保留 control/platform），从而吸收布局差异。

### 4.2 KeyBinding 与 KeyContext：一条绑定规则与上下文小语言

#### 4.2.1 概念说明

**`KeyBinding`** 是键位表里的一条规则，回答「什么键、在什么上下文、触发什么动作」：

- `keystrokes`：一个 `SmallVec`，长度 1 是单键，更长就是多键序列（`"cmd-k left"` 拆成两段，空格分隔）。
- `action`：类型擦除的动作实例（带数据的动作在这里携带序列化好的参数）。
- `context_predicate`：可选的上下文谓词——不写就是全局绑定。
- `meta`：来源编号，Zed 用它区分 user / vim / base / default 四层键位表，决定「禁用规则能压过谁」。

**`KeyContext`** 是元素树某个位置声明的「事实集合」，由一串条目组成：要么是纯标识符（`Editor`），要么是键值对（`mode = full`）。**注意区分两个容易混淆的东西**：

- `KeyContext`：元素在 paint 阶段声明的事实（「我是 Editor，mode 是 full」）。
- `KeyBindingContextPredicate`：绑定上的**谓词表达式**（「Editor 且 mode == full 时才启用我」），是一门支持 `&&`、`||`、`!`、`==`、`!=` 和 `>`（祖先 > 后代）的小语言。

从根元素到焦点元素的路径上，各节点声明的 `KeyContext` 依次堆叠成**上下文栈**（浅 → 深），谓词在这个栈上求值。

#### 4.2.2 核心流程

`KeyContext::parse` 的语法极简：空白分隔的条目序列，每条是 `identifier` 或 `key = value`，如 `"StatusBar mode = visible"`。

谓词小语言的运算符优先级（从低到高）：

```text
>(祖先-后代)  <  ||  <  &&  <  ==/!=  <  !
```

求值规则（`eval_inner`，针对「从浅到深排列的上下文栈」）：

- `Identifier(name)`：**栈顶**（最深上下文）包含该标识符才匹配。
- `Equal(k, v)` / `NotEqual(k, v)`：检查栈顶的键值对；`NotEqual` 在键不存在时视为真。
- `Not(p)`：对**整个栈的每个前缀**求值 `p`，任一匹配则 `Not` 失败（否定是全栈语义，不是只看栈顶）。
- `Descendant(parent, child)`：存在某个切分点，使 `parent` 匹配切分点之前的前缀、`child` 匹配之后的部分——即「祖先上下文匹配 parent，且其后代上下文匹配 child」。
- `And` / `Or`：常规逻辑组合，在栈顶求值。

#### 4.2.3 源码精读

`KeyBinding` 的字段定义：[src/keymap/binding.rs:10-17](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/binding.rs#L10-L17)——注意 `keystrokes` 是 `SmallVec<[KeybindingKeystroke; 2]>`（绝大多数绑定是 1~2 键，栈上分配），`context_predicate` 是 `Option<Rc<…>>`（共享、克隆廉价），`meta` 是 4.3 节优先级算法的来源分层依据。

`KeyBinding::new` 的便捷构造：[src/keymap/binding.rs:31-45](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/binding.rs#L31-L45)——第三个参数 `Option<&str>` 就是谓词字符串（如 `Some("editor && mode==full")`），内部立即用 `KeyBindingContextPredicate::parse` 解析。而 `load`：[src/keymap/binding.rs:48-75](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/binding.rs#L48-L75) 是 JSON 键位表加载时的入口，支持 `use_key_equivalents` 与平台键盘映射器。

`KeyContext` 结构与解析：[src/keymap/context.rs:9-10](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L9-L10)、[src/keymap/context.rs:61-95](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L61-L95)——`Vec<ContextEntry>`，每个条目是 `key: SharedString` 加 `Option<value>`；`parse_expr` 是一个手写的递归下降，逐条消费标识符或键值对。`new_with_defaults`：[src/keymap/context.rs:31-47](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L31-L47) 会预置 `os = macos/linux/windows`，这就是键位表里能写 `os == macos` 谓词的原因。

谓词枚举：[src/keymap/context.rs:172-197](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L172-L197)——七种节点；其 `parse` 入口与文档：[src/keymap/context.rs:220-257](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L220-L257)（文档注释里给了 `StatusBar > mode == visible` 的示例）。

求值核心 `eval_inner`：[src/keymap/context.rs:277-324](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L277-L324)——所有分支都只对 `contexts.last()`（栈顶）做包含检查，唯独 `Not` 与 `Descendant` 是栈感知的；代码里保留的注释（`// Workspace > Pane > Editor`）正是 Zed 真实的上下文栈形态。

优先级爬升解析器 `parse_expr`：[src/keymap/context.rs:350-379](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L350-L379)，常量表在 [src/keymap/context.rs:494-498](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L494-L498)。

#### 4.2.4 代码实践

1. **实践目标**：验证谓词小语言的解析与求值，特别是 `>` 算子。
2. **操作步骤**：运行 context 模块的单元测试：
   ```bash
   cargo test -p gpui keymap::context::tests
   ```
   重点阅读 [src/keymap/context.rs:701-734](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L701-L734) 的 `test_child_operator` 与 [src/keymap/context.rs:737-812](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L737-L812) 的 `test_not_operator`。
3. **需要观察的现象**：`"parent > child"` 在栈 `[parent, child]` 与 `[grandparent, parent, child]` 上都匹配，在 `[other, child]` 上不匹配；`"!editor"` 只要栈中**任何一层**出现 `editor` 就不匹配。
4. **预期结果**：两组测试全部通过；`test_child_operator` 里 `child > child`（同一标识符作为父子）在两层同名的栈上能匹配，单层不能。
5. 「待本地验证」：无——上述断言就是这两个测试函数里逐行写明的内容，跑测试即可确认。

#### 4.2.5 小练习与答案

**练习 1**：写出匹配「Workspace 之下的 Pane 之下的 Editor，且 vim 模式为 normal」的谓词字符串。
**答案**：`"Workspace > Pane > Editor && vim_mode == normal"`。注意 `&&` 优先级高于 `>` 之外的组合方式：由于 `==` 优先级最高，`vim_mode == normal` 先结合，再与左侧 `Descendant` 链做 `&&`。

**练习 2**：绑定 A 的谓词是 `"Editor"`，绑定 B 的谓词是 `"Editor && mode == full"`，上下文栈是 `["Editor mode == full"]`，两者都能匹配吗？
**答案**：都能。`Identifier("Editor")` 检查栈顶包含 `Editor`；`And` 的两个分支也都落在栈顶这一个上下文上求值。区别在**匹配深度与更浅栈下的行为**：栈为 `["Pane"]` 时 A、B 都失败；而 `is_superset`（[src/keymap/context.rs:328-348](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L328-L348)）判定「A 覆盖的范围 ⊇ B」，这正是 4.3 节里「在 broad 上下文写 null 能禁用 narrow 绑定」的判定基础。

**练习 3**：`key_context("Search mode=insert")` 中 `mode=insert` 是谓词吗？
**答案**：不是。元素侧 `key_context` 声明的是 `KeyContext`（事实），键值对 `mode=insert` 是其中一条 `ContextEntry`；谓词（`KeyBindingContextPredicate`）只出现在**绑定侧**，形如 `KeyBinding::new("enter", Confirm, Some("Search && mode == insert"))`。两边语法相似但角色不同，是初学最易混淆的点。

### 4.3 Keymap：优先级算法（深度优先、后加优先、来源分层）

#### 4.3.1 概念说明

`Keymap` 是全部 `KeyBinding` 的容器，挂在 `App` 上（不随窗口走），核心问题是：**同一个按键有多条绑定都命中时，谁说了算？**

GPUI 的答案是一个三级排序规则（文档注释写得很清楚，见 [src/keymap.rs:150-164](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L150-L164)）：

1. **深度优先**：绑定匹配的上下文越深（越靠近焦点）优先级越高——Editor 的绑定压过 Pane，Pane 压过 Workspace。没有谓词的绑定视为「与最深上下文同深」。
2. **同深度后加优先**：同深度时，后加入 keymap 的绑定优先。用户键位表在内置键位表之后加载，所以用户的改动天然胜出。
3. **来源分层（meta）**：`"x": null`（`NoAction`）这类禁用规则只能压制「同层或更弱来源」的绑定——基础键位表的 null 藏得住默认绑定，但藏不住用户绑定。

此外还有两种特殊的「负向动作」：`NoAction`（JSON 键位表里把某键设为 `null`，整个按键禁用）与 `Unbind("action_name")`（定向解绑某个动作）。它们的抑制同样遵循上述优先级，因此可以做到「只在某个上下文里禁用某键」。

#### 4.3.2 核心流程

`bindings_for_input(input, context_stack) -> (bindings, pending)` 的算法：

```text
1. 逆序遍历所有绑定（后加的先看）：
   a. binding_enabled：谓词在上下文栈上求 depth_of；
      无谓词 → depth = 栈长度（视为最深）；不匹配 → 跳过
   b. match_keystrokes：None → 跳过；Some(true) → 进 pending 池；Some(false) → 进 matched 池（记录 depth 与下标）
2. matched 池排序：depth 降序，同 depth 按加入顺序下标降序
   （即 (depth, ix) 双关键字都取大者在前）
3. 依序消费 matched：
   - NoAction：记下当前最小的来源 meta（no_action_meta），后续 meta >= 它的绑定被压制
   - Unbind：进入 unbound 列表，压制「同键且动作同名」的后续绑定
   - 普通绑定：通过压制检查后输出
4. pending 池：仅当下标比第一条已输出绑定更小（更早加入）时才保留为 pending
   ——否则一个已被更深绑定消费的键不该再挂起等待
```

用排序比较器写就是：

\[ \text{binding}_a \succ \text{binding}_b \iff (\text{depth}_a > \text{depth}_b) \lor (\text{depth}_a = \text{depth}_b \land \text{ix}_a > \text{ix}_b) \]

#### 4.3.3 源码精读

`Keymap` 结构：[src/keymap.rs:17-23](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L17-L23)——`bindings` 主表、`binding_indices_by_action_id`（按动作 `TypeId` 反查，服务于「显示某动作的快捷键」）、`disabled_binding_indices`（NoAction/Unbind 单独记账）、`version`（增删绑定时递增，供缓存失效判断）。

`add_bindings`：[src/keymap.rs:63-78](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L63-L78)——普通绑定进动作索引，禁用型绑定进 `disabled_binding_indices`，最后 `version +1`。

核心匹配 `bindings_for_input`：[src/keymap.rs:165-243](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L165-L243)。三个关键片段：

- 逆序遍历并按 `binding_enabled` 分流：[src/keymap.rs:173-186](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L173-L186)。
- 双关键字排序：[src/keymap.rs:188-190](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L188-L190)——`depth_b.cmp(depth_a).then(ix_b.cmp(ix_a))`，与上面的公式一一对应。
- NoAction 的来源分层压制：[src/keymap.rs:201-226](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L201-L226)——`no_action_meta` 取已见 NoAction 的最小 meta，后续 `meta >= no_action_meta` 的绑定被丢弃；没有 meta 的绑定按 user 层（0）处理。

`binding_enabled`：[src/keymap.rs:244-252](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L244-L252)——有谓词时返回 `predicate.depth_of(contexts)`（`depth_of` 在 [src/keymap/context.rs:259-268](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap/context.rs#L259-L268)，从最深前缀往浅试，返回第一个能让谓词成立的前缀长度）；无谓词返回栈长度。

行为参照测试 `test_depth_precedence`：[src/keymap.rs:346-367](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L346-L367)——栈 `[pane, editor]` 下同一按键返回 `[editor 绑定, pane 绑定]`；来源分层参照 `test_disable_weaker_sources_only`：[src/keymap.rs:574-641](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L574-L641)。

App 侧入口 `bind_keys`：[src/app.rs:2215-2218](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L2215-L2218)——写入 keymap 后追加 `Effect::RefreshWindows`，因为键位变化可能改变哪些视图显示的快捷键文案。keymap 字段定义与创建在 [src/app.rs:699](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L699)、[src/app.rs:829](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app.rs#L829)。

#### 4.3.4 代码实践

1. **实践目标**：用最小代码验证三级优先级规则（不改源码，靠读测试 + 跑测试）。
2. **操作步骤**：
   ```bash
   cargo test -p gpui test_depth_precedence test_context_precedence_with_same_source test_source_precedence_sorting
   ```
   对应 [src/keymap.rs:346-367](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L346-L367)、[src/keymap.rs:776-801](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L776-L801)、[src/keymap.rs:900-926](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L900-L926)。
3. **需要观察的现象**：三条测试分别覆盖「深度优先」「同深度后加优先」「同深度时 user(meta=0) 压过 default(meta=3)」。
4. **预期结果**：三条全部通过。把 `test_context_precedence_with_same_source`（[src/keymap.rs:776-801](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L776-L801)）里两条绑定的**添加顺序对调**（在脑中或本地副本做），可推演出断言会反转——同深度时后加者胜。
5. 「待本地验证」：对调实验需要改源码副本，本讲不修改仓库文件，故标注为待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：全局绑定（无谓词）和 `Some("Editor")` 绑定同键同深度同时命中，谁先？
**答案**：看 `binding_enabled`：无谓词返回 `contexts.len()`，`"Editor"` 谓词的 `depth_of` 在栈 `[Workspace, Editor]` 上至多返回 2——两者深度相同，于是落到第二级规则：后加入 keymap 的胜出。

**练习 2**：为什么「在 Workspace 层写 null」禁不掉「Editor 层的绑定」？
**答案**：见 `test_fail_to_disable`：[src/keymap.rs:644-664](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L644-L664)。NoAction 排序后位于更浅的 depth，而抑制检查只对**排序在它之后**（深度更浅或同深度更早加入）的绑定生效——Editor 绑定排在它前面，早已输出。要在 Editor 层禁用就得写 `Some("Editor")` 的 null（`test_disable_deeper`，[src/keymap.rs:666-686](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L666-L686)）。

**练习 3**：`bindings_for_action`（按动作反查）和 `bindings_for_input`（按按键正查）分别服务什么场景？
**答案**：`bindings_for_input` 是按键派发主链路用的（4.5 节）；`bindings_for_action` 服务 UI 显示（按钮上要印快捷键），它借助 `binding_indices_by_action_id` 索引避免全表扫描，并用 `is_superset` 判断某条禁用规则是否覆盖该绑定，见 [src/keymap.rs:95-134](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L95-L134)。

### 4.4 DispatchTree：每帧重建的监听器树

#### 4.4.1 概念说明

`Keymap` 只回答「这个键在这些上下文下对应哪些动作」，但**动作要派发给谁**需要一个数据结构来承载：哪个元素注册了 `on_action`、哪个节点声明了哪个 `KeyContext`、焦点落在哪个节点。这就是 `DispatchTree`。

设计要点：

- **平行树**：`DispatchTree` 的节点与元素树一一对应（每个元素 prepaint 时 push 一个节点），但只保留派发所需的信息：监听器闭包、`KeyContext`、`FocusId`、`view_id`、父节点。
- **每帧重建**：与元素树同步。监听器闭包只活一帧（和鼠标监听器一样，见 u5-l2），这换来了「零陈旧引用」——元素消失，其监听器自动失效。
- **双缓冲**：`rendered_frame` 与 `next_frame` 各持一棵（[src/window.rs:1848-1849](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L1848-L1849)），按键事件在**上一帧绘制完成的树**上派发。
- **`DispatchNodeId` 跨帧不稳定**：节点就是 `Vec` 下标，树重排后下标含义改变，所以 ID 只在本帧内有效（结构体文档明确警告，[src/key_dispatch.rs:66-69](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L66-L69)）。

#### 4.4.2 核心流程

**构建（绘制期间）**：

```text
prepaint: 每个元素进入时 push_node()，退出时 pop_node()
          元素内调用 window.set_focus_handle / set_view_id 记录焦点/视图
paint:    元素进入时 set_active_node(该元素 prepaint 时拿到的 node_id)
          元素内调用 window.set_key_context(...) 声明上下文
                   window.on_action(type_id, listener) 注册动作监听
                   window.on_key_event(...) 注册原始键盘监听
帧末:     next_frame 与 rendered_frame 交换，旧树被下一帧覆盖
```

注意 `context_stack`（`DispatchTree` 自己维护的栈）在构建期间随 push/pop 起伏，用于**构建时**的上下文感知；而按键派发时的上下文栈是**从 dispatch_path 现算的**（见 `bindings_for_input`，[src/key_dispatch.rs:448-463](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L448-L463)）——沿派发路径把各节点的 `context` 收集起来。

**查询（派发期间）**：

- `dispatch_path(target)`：从任意节点沿 `parent` 指针爬到根再反转，得到「根 → 目标」的节点序列；目标通常是焦点所在节点（无焦点时回退到根节点）。
- `bindings_for_input`（树上的版本）：把 dispatch_path 上各节点的 `KeyContext` 收集成栈，交给 `Keymap` 匹配。

**复用优化**：启用了 `entity.cached()` 的视图跳过重绘时，它上一帧贡献的整棵子树要原样搬进新树——`reuse_subtree` 把旧区间内的节点（连同监听器闭包）逐节点 `move_node` 到新树对应位置，并通过 `ReusedSubtree::refresh_node_id` 修正 ID 映射。这是 u4-l3 讲过的「缓存重放」在派发树上的对应物。

#### 4.4.3 源码精读

`DispatchTree` 与 `DispatchNode` 结构：[src/key_dispatch.rs:71-91](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L71-L91)——节点持有三类监听器（原始键盘、动作、修饰键变化）加 `context`/`focus_id`/`view_id`/`parent`；树侧还有 `focusable_node_ids`（FocusId → 节点）与 `view_node_ids`（EntityId → 节点）两张索引表，以及共享的 `keymap: Rc<RefCell<Keymap>>`——**派发树与 App 共享同一个 keymap 实例**，所以 `bind_keys` 之后无需通知任何窗口去刷新引用。

节点入栈/出栈：[src/key_dispatch.rs:166-176](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L166-L176)（`push_node` 记录父节点并压栈）。元素侧的调用点：[src/element.rs:403-412](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/element.rs#L403-L412)（prepaint 包裹）与 [src/element.rs:477](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/element.rs#L477)（paint 恢复活跃节点）。

`set_active_node`：[src/key_dispatch.rs:178-213](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L178-L213)——把活跃位置切换到指定节点；若该节点的父不在当前栈上（延迟绘制的浮层常见），则沿 parent 链重建整条栈。

三个注册方法的窗口侧门面（都有 `debug_assert_paint` 断言，只能在 paint 阶段调用）：`set_key_context` [src/window.rs:4759-4762](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L4759-L4762)、`on_action` [src/window.rs:5973-5983](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5973-L5983)、`set_focus_handle`（prepaint）[src/window.rs:4768-4774](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L4768-L4774)。它们分别转调 `dispatch_tree.set_key_context` / `on_action` / `set_focus_id`（[src/key_dispatch.rs:215-224](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L215-L224)、[src/key_dispatch.rs:333-344](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L333-L344)）。

div 的落地：`key_context()` 链式方法把字符串解析后存进 `Interactivity.key_context` 字段（[src/elements/div.rs:794-803](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L794-L803)，字段定义 [src/elements/div.rs:2035](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L2035)）；paint 阶段 `paint_keyboard_listeners` 统一写树：[src/elements/div.rs:3138-3168](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L3138-L3168)——先 `window.set_key_context`，再逐个注册 key down/up、modifiers changed、action 监听。

`dispatch_path`：[src/key_dispatch.rs:563-572](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L563-L572)；树上的 `bindings_for_input`（收集上下文栈后委托 Keymap）：[src/key_dispatch.rs:448-463](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L448-L463)。

复用：`ReusedSubtree` 与 `refresh_node_id` [src/key_dispatch.rs:93-113](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L93-L113)、`reuse_subtree`/`move_node` [src/key_dispatch.rs:246-308](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L246-L308)——`move_node` 用 `mem::take` 把旧节点的监听器**搬运**（而非克隆）到新节点。

#### 4.4.4 代码实践

1. **实践目标**：确认「key_context 写在哪个元素上，就落进派发路径上对应节点的 context 里」。
2. **操作步骤**：运行 [examples/testing.rs](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/testing.rs) 应用：
   ```bash
   cargo run -p gpui --example testing
   ```
   对照其 render 实现 [examples/testing.rs:80-93](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/testing.rs#L80-L93)：根 div 上 `.key_context("Counter")` + `.on_action(...)` + `.track_focus(...)` 三件套。
3. **需要观察的现象**：按 ↑/↓ 计数变化。结合 [examples/testing.rs:182-185](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/testing.rs#L182-L185) 的 `bind_keys`（`"up"` → `Increment`，上下文 `Some("Counter")`），说明该绑定只有当焦点在声明了 `Counter` 上下文的子树内才可能命中。
4. **预期结果**：↑ 加一、↓ 减一；点击按钮同样生效（按钮走的是鼠标路径，与键位无关）。
5. 「待本地验证」：想进一步看「焦点不在该子树时按键失效」，可在本地把 `track_focus` 挪到一个不含 `key_context` 的内层 div 上再按 ↑——预期失效。此实验需改示例源码，故标注待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `on_action` 的监听器必须每帧重新注册，而 `keymap` 不用？
**答案**：监听器闭包捕获了元素当帧的状态（通常经 `cx.listener` 绑定了实体引用），元素树每帧重建，旧的闭包随旧帧作废；keymap 是纯数据（按键 → 动作 + 谓词），与帧无关，挂在 `App` 上由所有窗口共享（[src/key_dispatch.rs:78](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L78)）。

**练习 2**：`dispatch_path` 返回的顺序是什么？为什么按键派发要用这个顺序？
**答案**：根 → 焦点节点（[src/key_dispatch.rs:570](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L570) 反转后）。四阶段派发需要「捕获阶段沿根向焦点、冒泡阶段反向」，有序路径让两个方向都只是一个正/反遍历。

**练习 3**：`reuse_subtree` 里为什么用 `mem::take` 搬运监听器而不是 clone？
**答案**：监听器是 `Rc<dyn Fn>`，clone 本可共享；但旧树即将被丢弃，take（[src/key_dispatch.rs:259-261](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L259-L261)）语义上明确了「所有权转移」——旧节点自此是空壳，杜绝两棵树同时持有一份监听器列表导致的重复派发隐患。

### 4.5 一次按键的完整旅程与四阶段动作派发

#### 4.5.1 概念说明

现在把四个部件串成完整链路。从平台送来一个 `KeyDownEvent` 到某个 `on_action` 闭包执行，中间依次经过：

1. **事件入口**：`Window::dispatch_event` 识别出键盘事件，转 `dispatch_key_event`。
2. **焦点定位**：在**上一帧**的派发树上找到焦点节点，算出 `dispatch_path`。
3. **拦截器**：`intercept_keystrokes` 注册的全局拦截器先跑（可用 `stop_propagation` 掐断后续一切）。
4. **匹配**：`dispatch_tree.dispatch_key` 把按键（连同之前 pending 的序列）交给 `Keymap::bindings_for_input`，得到「立即可执行的绑定列表」或「pending 标记」。
5. **pending 处理**：多键序列进行中则暂存输入并启动 1 秒超时；超时后 `flush_dispatch` 把已敲的键按最长可执行前缀重放。
6. **动作派发**：对每条命中的绑定调用 `dispatch_action_on_node`，走**四阶段**派发；任一阶段可 `stop_propagation`。
7. **兜底**：若没有任何绑定消费这次按键，则走原始 `KeyDownEvent` 监听器（`on_key_down`）与观察者，最后还可能交给文本输入处理器（IME 路径，见 u5-l1）。

**四阶段**（与 u5-l3 呼应，这里给出源码级细节）：

| 阶段 | 参与者 | 传播方向 | 停止传播默认值 |
| --- | --- | --- | --- |
| 1 全局捕获 | `App::on_action` 全局监听器 | 注册顺序 | 不停止 |
| 2 路径捕获 | 派发路径上各节点的监听器 | 根 → 焦点 | 不停止 |
| 3 路径冒泡 | 派发路径上各节点的监听器 | 焦点 → 根 | **默认停止**（命中即停） |
| 4 全局冒泡 | 全局监听器 | 逆注册顺序 | **默认停止** |

「冒泡默认停止」是动作派发与鼠标事件派发的关键差异（鼠标 Bubble 阶段不停止）：这保证了多条绑定命中时只有优先级最高的一条真正执行，其余的被跳过——与 4.3 的排序规则配套。

#### 4.5.2 核心流程

`dispatch_key`（多键序列状态机，树侧）的决策树：

```text
输入 = 之前的 pending 序列 + 新按键
    │
    ├─ 匹配后仍 pending（可能是更长序列的前缀）
    │     → 返回 { pending: input }，窗口暂存并（按需）启动超时
    │
    ├─ 有立即可执行的绑定
    │     → 返回 { bindings }，窗口逐条派发
    │
    ├─ 输入只有 1 个键且无绑定
    │     → 返回空结果（走兜底：原始键盘监听/文本输入）
    │
    └─ 输入多个键且整体不匹配（序列中断）
          → 弹出新键，replay_prefix 找出「已敲键里最长的可执行前缀」
            先重放，再用剩余后缀递归 dispatch_key
```

窗口侧超时重放：pending 输入存在 `PendingInput`（含 keystrokes、focus、timer、needs_timeout）；若序列本身已能构成某绑定（`pending_has_binding`）或焦点处有文本输入，则启动 1 秒定时器，到点后 `flush_dispatch` 把 pending 转为重放事件。**焦点一变，旧 pending 立即作废**（focus 不匹配则重置）。

#### 4.5.3 源码精读

事件分流到键盘分支：[src/window.rs:5130-5132](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5130-L5132)——`dispatch_event` 把 `mouse_event` 与 `keyboard_event` 分别路由。

`dispatch_key_event` 主体：[src/window.rs:5239-5407](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5239-L5407)。分段解读：

- **先画脏帧再派发**：[src/window.rs:5240-5242](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5240-L5242)——派发必须在「与屏幕一致的帧」上进行，否则键位可能基于过时的上下文栈；随后用焦点定位节点并计算 `dispatch_path`：[src/window.rs:5244-5245](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5244-L5245)。
- **拦截器**：[src/window.rs:5297-5302](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5297-L5302)。
- **旧 pending 作废**：[src/window.rs:5304-5307](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5304-L5307)——焦点变了，跨焦点的序列没有意义。
- **调用匹配状态机**：[src/window.rs:5309-5313](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5309-L5313)。
- **pending 分支与 1 秒超时**：[src/window.rs:5320-5371](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5320-L5371)——`Duration::from_secs(1)` 的定时任务到期后调 `flush_dispatch`（[src/window.rs:5354-5357](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5354-L5357)）并重放。
- **逐条派发绑定**：[src/window.rs:5389-5403](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5389-L5403)——每条绑定调 `dispatch_action_on_node`，一旦 `propagate_event` 为 false（被消费）即通知观察者并返回。

树侧状态机 `DispatchTree::dispatch_key`：[src/key_dispatch.rs:483-519](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L483-L519)——与上面的决策树逐分支对应；`replay_prefix`：[src/key_dispatch.rs:538-561](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L538-L561) 从最长前缀往短试，找出最后一个能命中绑定的前缀作为重放单位。

四阶段派发 `dispatch_action_on_node_inner`：[src/window.rs:5573-5686](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5573-L5686)：

- 全局捕获：[src/window.rs:5581-5606](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5581-L5606)。
- 路径捕获（沿 dispatch_path 正序）：[src/window.rs:5612-5633](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5612-L5633)。
- 路径冒泡（逆序，命中即 `cx.propagate_event = false`）：[src/window.rs:5635-5657](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5635-L5657)——注意 [src/window.rs:5645](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5645) 的注释「Actions stop propagation by default during the bubble phase」。
- 全局冒泡：[src/window.rs:5659-5685](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5659-L5685)。

**排查「按键没反应」的路径**（顺着链路反向查）：

1. 焦点在目标子树吗？——`track_focus` 是否生效、`window.focus(...)` 是否调用。派发路径由焦点决定（[src/window.rs:5547-5555](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5547-L5555)：无焦点回退根节点）。
2. 上下文栈里有所需的上下文吗？——`key_context` 是否写在**包含焦点节点**的祖先链上；拼错字符串（`KeyContext::parse` 失败）会被 `log_err` 静默吞掉（[src/elements/div.rs:799-801](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/elements/div.rs#L799-L801)），不 panic！
3. 绑定真的注册了吗？——`cx.bind_keys` 是否在 `Application::run` 回调里执行过；谓词字符串能否解析（解析失败会在加载期报错）。
4. 是不是被更深上下文的同键绑定截胡了？——冒泡默认停止，优先级更高的绑定先消费按键，后面的不再执行。
5. 是不是 pending 了？——`window.has_pending_keystrokes()` 可查（[src/window.rs:5495-5497](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5495-L5497)）；`cmd-k` 可能正在等下一个键。
6. 键盘布局/IME 是否吞了键？——`should_match` 的 `key_char` 分支与 `prefer_character_input` 文本优先逻辑（[src/window.rs:5373-5387](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5373-L5387)）。

#### 4.5.4 代码实践

1. **实践目标**：观察「多条绑定命中时只有最高优先级者执行」与「无绑定时走兜底」。
2. **操作步骤**：key_dispatch 模块自带集成测试：
   ```bash
   cargo test -p gpui key_dispatch::
   ```
   重点读 [src/key_dispatch.rs:793-830](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L793-L830) 的 `test_pending_has_binding_state`——它手工 `push_node` + `set_key_context("ContextB")` 搭出一棵最小派发树，然后逐步 `dispatch_key`，断言 pending/bindings 状态迁移。
3. **需要观察的现象**：`ctrl-b` 敲下后 `result.pending.len() == 1`（序列进行中）；补上 `h` 后 pending 清空、`bindings.len() == 1`（命中 `ctrl-b h`）。
4. **预期结果**：测试通过。这个测试同时演示了 DispatchTree 可以脱离真实窗口手工构建——非常适合做行为实验。
5. 「待本地验证」：无。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `dispatch_key_event` 开头要先 `draw` 脏帧？
**答案**：见 [src/window.rs:5240-5242](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5240-L5242)。键位匹配依赖上一帧派发树里的上下文栈与监听器注册；若刚发生过状态更新（树已脏）还没重绘，旧树的上下文可能已经不能代表当前 UI（例如焦点刚移进了一个新面板），基于旧树派发会把键送给错误的目标。

**练习 2**：用户敲了 `ctrl-b x`，而绑定表里只有 `ctrl-b h`，`ctrl-b` 敲下时没有任何单键绑定，最终会发生什么？
**答案**：`ctrl-b` 敲下 → `dispatch_key` 返回 pending → 窗口存 `PendingInput`；`x` 敲下 → 整体不匹配且输入长度 > 1 → `replay_prefix` 找最长可执行前缀（`ctrl-b` 无绑定）→ 把 `ctrl-b` 作为无绑定的 `Replay` 重放（走原始键盘监听/文本输入路径），`x` 再单独递归处理。`x` 会正常输入到文本框。见 [src/key_dispatch.rs:505-518](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L505-L518) 与 `replay_pending_input` [src/window.rs:5509-5545](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5509-L5545)。

**练习 3**：同层两份键位表（default 与 user）都给 `cmd-r` 绑了不同动作，都注册了 `on_action` 吗？会执行几次？
**答案**：`Keymap::bindings_for_input` 会把两条都返回（user 在前），但窗口循环派发时（[src/window.rs:5390-5402](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/window.rs#L5390-L5402)）第一条 user 绑定的动作在冒泡阶段默认停止传播，循环随即退出——**只有 user 动作执行一次**。`on_action` 注册在元素上而不是绑定上，与几条绑定无关。

## 5. 综合实践

**任务**：构建一个「外层 Editor、内层 Search」的嵌套上下文界面，给同名动作 `Toggle` 在两个上下文绑定不同按键，验证焦点移动时同一按键的行为切换。这正是 Zed 里「搜索框抢走按键」的最小模型。

**第一步：创建示例文件**（示例代码）。在 `examples/` 下新建 `key_context_toggle.rs`：

```rust
// 示例代码：保存为 examples/key_context_toggle.rs
#![cfg_attr(target_family = "wasm", no_main)]

use gpui::{
    actions, div, prelude::*, App, Context, FocusHandle, Focusable, Render, SharedString,
    Window, WindowOptions,
};
use gpui_platform::application;

actions!(demo, [Toggle]);

/// 外层：编辑器面板，key_context 为 "Editor"
struct EditorPanel {
    search: gpui::Entity<SearchBox>,
    focus_handle: FocusHandle,
    log: SharedString,
}

impl Focusable for EditorPanel {
    fn focus_handle(&self, _cx: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

impl Render for EditorPanel {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .id("editor")
            // 外层上下文：Editor
            .key_context("Editor")
            .on_action(cx.listener(|this: &mut Self, _: &Toggle, _, cx| {
                this.log = "Editor 收到 Toggle（cmd-t）".into();
                cx.notify();
            }))
            .track_focus(&self.focus_handle)
            .flex()
            .flex_col()
            .gap_4()
            .p_6()
            .size_full()
            .child(self.search.clone())
            .child(div().child(self.log.clone()))
            .child(div().text_color(gpui::rgb(0x888888)).child(
                "焦点在编辑器: cmd-t / cmd-f；焦点在搜索框: cmd-t（按 tab 切换焦点）",
            ))
    }
}

/// 内层：搜索框，key_context 为 "Search"
struct SearchBox {
    focus_handle: FocusHandle,
    log: SharedString,
}

impl Focusable for SearchBox {
    fn focus_handle(&self, _cx: &App) -> FocusHandle {
        self.focus_handle.clone()
    }
}

impl Render for SearchBox {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .id("search")
            // 内层上下文：Search——焦点在此时的上下文栈是 [Editor, Search]
            .key_context("Search")
            .on_action(cx.listener(|this: &mut Self, _: &Toggle, _, cx| {
                this.log = "Search 收到 Toggle（cmd-t）".into();
                cx.notify();
            }))
            .track_focus(&self.focus_handle)
            .p_2()
            .rounded_md()
            .border_1()
            .when(!self.log.is_empty(), |el| el.child(self.log.clone()))
            .when(self.log.is_empty(), |el| {
                el.child("搜索框（点击或按 tab 聚焦）")
            })
    }
}

fn main() {
    application().run(|cx: &mut App| {
        // 同名动作、同一按键、两个上下文：焦点决定哪条生效。
        // Search 在栈的更深处（depth 2 > depth 1），焦点在搜索框时它必然胜出。
        // 注意 tab 切换焦点无需手动绑定：tab 导航由焦点系统内置处理（u5-l5 详解）。
        cx.bind_keys([
            gpui::KeyBinding::new("cmd-t", Toggle, Some("Editor")),
            gpui::KeyBinding::new("cmd-t", Toggle, Some("Search")),
        ]);
        cx.open_window(WindowOptions::default(), |window, cx| {
            let search = cx.new(|cx| SearchBox {
                focus_handle: cx.focus_handle(),
                log: "".into(),
            });
            let editor = cx.new(|cx| EditorPanel {
                search: search.clone(),
                focus_handle: cx.focus_handle(),
                log: "".into(),
            });
            editor.focus_handle(cx).focus(window, cx);
            editor
        })
        .unwrap();
    });
}
```

**第二步：注册并运行**。本仓库的 gpui `Cargo.toml` 显式列出了全部 `[[example]]`（如 [Cargo.toml:167-169](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/Cargo.toml#L167-L169) 的 hello_world）。两种做法：

- 做法一（不动 Cargo.toml）：依赖 Cargo 的 autoexamples 自动发现，直接 `cargo run -p gpui --example key_context_toggle`。若提示找不到 target，改做法二。**待本地验证**。
- 做法二（与仓库一致）：仿照现有条目在 `Cargo.toml` 加三行 `[[example]] name/path`，再运行同一条命令。

**第三步：观察三件事**：

1. 焦点在编辑器面板时按 `cmd-t` → 「Editor 收到 Toggle」。此时上下文栈是 `["Editor"]`（深度 1），只有 Editor 绑定可用。
2. 点击搜索框（或按 tab）聚焦后再按 `cmd-t` → 「Search 收到 Toggle」。上下文栈变成 `["Editor", "Search"]`（深度 2），Search 绑定深度更深（`depth_of` 返回 2 对 1），排序在前；且动作冒泡默认停止传播，Editor 的监听器不会再执行。
3. 把两条绑定的上下文对调位置再试（本地实验）：无论焦点在哪，**深度规则不变**，行为不受添加顺序影响——这正是与「同深度靠后加优先」的区别（对照 [src/keymap.rs:776-801](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/keymap.rs#L776-L801) 的测试）。

**第四步（可选，测试化）**：仿照 [examples/testing.rs:251-273](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/examples/testing.rs#L251-L273) 的 `test_counter_in_window`，把上面的窗口逻辑搬进 `#[gpui::test]`，用 `cx.simulate_keystrokes("cmd-t")`（[src/app/test_context.rs:498](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/app/test_context.rs#L498)）分别在两种焦点状态下断言日志内容。这样不用肉眼盯窗口也能回归验证。**待本地验证**（需要 `--features test-support`）。

## 6. 本讲小结

- **Keystroke 是匹配的最底层原语**：`parse` 把 `"ctrl-shift-a"` 结构化；`should_match` 吸收非美式键盘与 IME 差异；`match_keystrokes` 的三态返回（不匹配 / pending / 命中）支撑单键与多键序列。
- **绑定与上下文是两种语言**：元素侧 `key_context` 声明 `KeyContext` 事实；绑定侧 `KeyBindingContextPredicate` 是支持 `&& || ! == != >` 的小语言，在「从浅到深」的上下文栈上求值。
- **Keymap 的优先级 = 三级排序**：匹配深度降序 → 加入顺序降序 → NoAction/Unbind 按 `meta` 来源分层压制。深层上下文、后加载的键位表（用户配置）天然胜出。
- **DispatchTree 是每帧重建的平行树**：prepaint push/pop 节点、paint 写入上下文与监听器、帧末双缓冲交换；`dispatch_path` 由**焦点**（而非鼠标）决定；`reuse_subtree` 让被缓存视图的监听器子树免于重建。
- **派发是四阶段状态机**：全局捕获 → 路径捕获 → 路径冒泡（命中即默认停止传播）→ 全局冒泡；多键序列由 `PendingInput` 暂存、1 秒超时后按最长可执行前缀重放，焦点一变即作废。
- **排查「按键没反应」六问**：焦点在吗 → 上下文链上有声明的上下文吗 → 绑定注册了吗 → 被更深的绑定截胡了吗 → 是否 pending 中 → 是否被 IME/文本输入优先吞掉。

## 7. 下一步学习建议

- **u5-l5 焦点系统**：本讲的 `dispatch_path` 完全由焦点位置决定，下一讲深入 `FocusHandle`、`FocusMap` 与 tab 导航（`tab_stop`），补齐 `track_focus` 背后的机制。
- **阅读 Zed 主仓的真实键位表**：`assets/keymaps/` 下的 JSON 就是 `KeyBinding::load` 的输入（`context` 字段即谓词字符串、`"x": null` 即 NoAction、meta 分层对应 default/base/user 多层文件），用本讲的算法逐一解释其行为。
- **顺手验证一个上游案例**：[src/key_dispatch.rs:740-790](https://github.com/zed-industries/zed/blob/1b04e4caf01e376624fb514ef85b0e6d8ee5d930/crates/gpui/src/key_dispatch.rs#L740-L790) 的 `test_overlay_after_base_restores_shadowed_picker_binding` 记录了一个「同深度靠加载顺序决胜」的真实缺陷（picker 预览页脚快捷键被 base 键位表遮蔽），读完它你会对三级排序里「后加优先」这一级有肌肉记忆。
- **再往后**：u7-l4（测试 GPUI 应用）会系统讲 `simulate_keystrokes` 与 `dispatch_action`，把本讲综合实践第四步的做法变成常规武器。
