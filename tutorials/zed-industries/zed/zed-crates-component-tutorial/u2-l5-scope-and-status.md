# ComponentScope 分类与 ComponentStatus 生命周期

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `ComponentScope` 的全部变体、它们的显示名（display name）与默认值 `None` 的特殊地位，并解释「未分类组件」在预览界面里去了哪里。
2. 为一个新组件挑选正确的作用域，并理解分组标题与分组排序都由 strum 序列化出的**显示名**决定，而不是 Rust 变体名。
3. 说出 `ComponentStatus` 四种状态（`Live`、`WorkInProgress`、`EngineeringReady`、`Deprecated`）各自的设计系统治理语义、对应的徽章颜色与 tooltip 文案。
4. 解释 strum 的 `Display` 与 `EnumString` 两个派生分别提供什么能力、在预览代码里哪些地方消费了 `Display`，以及 `EnumString`（字符串解析方向）目前的实际使用状况。

本讲承接 u2-l1 对 `Component` trait 七个方法的逐方法精读——那一讲我们知道了「一强烈（scope）」与「四随意（status）」的覆写建议，本讲把这两个方法背后的两个枚举彻底拆开。

## 2. 前置知识

- **枚举即分类法**：`ComponentScope` 与 `ComponentStatus` 都是普通 Rust 枚举，没有携带任何数据。它们的价值不在「存储什么」，而在「穷举了一整套受控词表」——组件作者只能从这份词表里选词，预览界面因此可以对所有组件做统一的分组、过滤与渲染。
- **显示名（display name）与变体名（variant name）的分离**：Rust 变体名要满足标识符规则（不能含空格和 `&`），而界面文案可以任意。strum 的 `#[strum(serialize = "...")]` 属性让同一个变体拥有一份「给人看的名字」，例如变体 `Input` 的显示名是 `Forms & Input`。
- **设计系统治理（design system governance）**：在一个多人共建的 UI 组件库里，需要回答「这个组件能不能用在正式功能里？」「它是不是快被删了？」这类问题。`ComponentStatus` 就是用一枚小徽章把答案挂在组件预览页的头部，让设计、工程两方对组件生命周期有共同认知。
- **`ComponentMetadata` 快照**（u2-l2 已建立）：注册时 `register_component` 会把 `T::scope()` 与 `T::status()` 各求值一次，作为两个字段抄录进 `ComponentMetadata`。预览界面读到的永远是这份快照，不再回调 trait。
- **strum**：一个专门「给枚举加本事」的过程宏 crate。Zed 工作区统一声明为 `strum = { version = "0.27.2", features = ["derive"] }`（见 [Cargo.toml:L818](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/Cargo.toml#L818)），component crate 经 `strum.workspace = true` 引入（见 [crates/component/Cargo.toml:L19](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/Cargo.toml#L19)）。本讲只涉及它的两个派生：`Display`（枚举 → 字符串）与 `EnumString`（字符串 → 枚举）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/component/src/component.rs` | 定义 `ComponentScope`、`ComponentStatus` 两个枚举，`Component` trait 中 `scope()`/`status()` 的默认值，以及 `ComponentStatus::description()` 的四段治理文案 |
| `crates/component_preview/src/component_preview.rs` | 消费端：`scope_ordered_entries` 按 scope 分组排序，`render_header`/`render_component_status` 渲染 scope 标签与 status 徽章，`filtered_components` 用 scope 显示名参与过滤 |
| `crates/ui/src/components/divider.rs` | 真实例证：`Divider` 覆写 `scope()` 返回 `Layout` |
| `crates/ui/src/components/notification/alert_modal.rs` | 真实例证：`AlertModal` 同时覆写 `scope()` 与 `status()`（目前组件树里唯一覆写 `status` 的组件） |
| `crates/ui/src/prelude.rs`、`crates/ui/src/component_prelude.rs` | 两个 prelude 对这两个枚举的导出差异：`ComponentScope` 在两边都有，`ComponentStatus` 只在 `component_prelude` |

## 4. 核心概念与源码讲解

### 4.1 ComponentScope：组件在设计系统目录里的「货架位置」

#### 4.1.1 概念说明

`ComponentScope` 回答的问题是：「这个组件属于哪一类 UI？」预览界面据此把上百个组件切分成左侧导航里一个个可浏览的分组，否则读者面对的将是一张按字母排序的巨长清单。

先纠正一个容易以讹传讹的数字：这个枚举实际有 **17 个变体**——16 个真实分类，外加 1 个特殊的 `None` 哨兵。`None` 不是分类，而是「组件作者没有覆写 `scope()`」时的默认值，语义是「还没归类」。

16 个真实分类及其 strum 显示名如下：

| 变体名 | 显示名 | 典型组件 |
| --- | --- | --- |
| `Agent` | `Agent` | `ai/thread_item.rs` 中的 AI 会话条目 |
| `Collaboration` | `Collaboration` | 协作相关组件 |
| `DataDisplay` | `Data Display` | `Callout` |
| `Editor` | `Editor` | 编辑器内组件 |
| `Images` | `Images & Icons` | `Image`、`DecoratedIcon` |
| `Input` | `Forms & Input` | `Button`、`IconButton`、`ToggleButton`、`NumberField` |
| `Layout` | `Layout & Structure` | `Divider` |
| `Loading` | `Loading & Progress` | 加载/进度类 |
| `Navigation` | `Navigation` | `Tab`、`TreeViewItem`、`ButtonLink` |
| `Notification` | `Notification` | `AlertModal`、`StatusToast` |
| `Overlays` | `Overlays & Layering` | 弹层类 |
| `Onboarding` | `Onboarding` | 引导流程 |
| `Status` | `Status` | `Indicator` |
| `Typography` | `Typography` | 文本类 |
| `Utilities` | `Utilities` | 工具类 |
| `VersionControl` | `Version Control` | 版本控制相关 |

注意三条规律：

1. 没加 `#[strum(serialize = "...")]` 属性的变体，显示名就是变体名本身（如 `Agent`、`Editor`）。
2. 加了属性的变体，显示名与变体名刻意分离：变体名短、适合写代码（`Input`），显示名长、适合给人看（`Forms & Input`）。
3. `None` 也有自己的显示名——`"Unsorted"`。这个细节后面会带来一个有趣的表现。

#### 4.1.2 核心流程

scope 从注册到渲染的完整链路：

```text
组件作者覆写 scope()            注册期（u2-l2 已讲）           预览期
─────────────────────         ─────────────────────         ──────────────────────────────
fn scope() -> ComponentScope   register_component::<T>()      scope_ordered_entries()
  ComponentScope::Input   ──►  求值一次,抄进                ──►  按 scope 分桶(HashMap)
                                     ComponentMetadata.scope       排除 None,按显示名字典序排组
                                                                    组内按 sort_name 排组件
                                                                    None 组件最后归入
                                                                    "Uncategorized" 分组
```

关键决策都在预览期这一侧：

1. **分组排序按显示名**：`scopes.sort_by_key(|s| s.to_string())`——所以 `Input` 分组排在 `Editor` 之后、`Images` 之前，因为它以显示名 `Forms & Input` 参与排序，首字母是 F 而不是 I。
2. **`None` 被排除在字典序之外**，单独处理，永远殿后，标题是写死的 `"Uncategorized"`。
3. **过滤也用显示名**：在搜索框输入 `forms` 能命中所有 `Input` 作用域的组件，因为过滤匹配的是 `scope().to_string()` 的小写形式。

#### 4.1.3 源码精读

先看枚举定义本身：

[crates/component/src/component.rs:L298-L325](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L298-L325) —— `ComponentScope` 的完整定义：17 个变体、6 处 `#[strum(serialize = "...")]` 属性、以及 `Debug, Clone, PartialEq, Eq, Hash, Display, EnumString` 七个派生。`PartialEq/Eq/Hash` 让它可以做 HashMap 的键（分组要用），`Display` 给出 `to_string()`，`EnumString` 给出 `FromStr`。

[crates/component/src/component.rs:L182-L184](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L182-L184) —— `Component` trait 中 `scope()` 的默认实现返回 `ComponentScope::None`：不写 `scope()` 的组件不会消失，而是落进未分类区。trait 的 doc 注释（第 167-169 行）明确建议「一般至少要实现 `scope` 和 `preview`」。

再看消费端。分组的核心是 `scope_ordered_entries`：

[crates/component_preview/src/component_preview.rs:L284-L290](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L284-L290) —— 从分桶的 HashMap 里取出所有 scope 键，用 `filter(|scope| !matches!(**scope, ComponentScope::None))` 把 `None` 摘出去，然后 `sort_by_key(|s| s.to_string())` 按显示名做字典序。这两行决定了左侧导航的分组顺序。

[crates/component_preview/src/component_preview.rs:L308-L320](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L308-L320) —— 未分类组件的归宿：在所有正常分组之后，追加一个分隔线和标题为 `"Uncategorized"` 的分组（注意这是硬编码字符串，不是 `scope.to_string()`），组内同样按 `sort_name` 排序。

[crates/component_preview/src/component_preview.rs:L296-L304](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L296-L304) —— 每个分组渲染为 `Separator` + `SectionHeader`（标题就是 `scope.to_string()`，即 strum 显示名）+ 一串组件条目。

scope 还出现在两个渲染位置，风格不同：

[crates/component_preview/src/component_preview.rs:L464-L468](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L464-L468) —— 「All Components」总览页里，组件名旁边以 `(Forms & Input)` 的形式追加 scope，但用 `.when(scope != ComponentScope::None, ...)` 挡掉了未分类组件——总览页里 None 组件不显示 scope 后缀。

[crates/component_preview/src/component_preview.rs:L951-L957](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L951-L957) —— 单个组件详情页的头部，scope 以小号弱色标签的形式出现在组件大标题上方。注意这里**没有** `None` 判断：由于 `None` 的 strum 显示名是 `"Unsorted"`，一个未分类组件的详情页头部会真的显示「Unsorted」三个词——这是「显示名与变体名分离」带来的一个真实怪癖，读源码时值得会心一笑。

最后看三个真实组件的覆写：

[crates/ui/src/components/divider.rs:L162-L165](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L162-L165) —— `Divider` 返回 `ComponentScope::Layout`，于是它出现在「Layout & Structure」分组。

[crates/ui/src/components/button/button.rs:L516-L518](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/button.rs#L516-L518) —— `Button` 返回 `ComponentScope::Input`。整个按钮家族（`IconButton`、`CopyButton`、`ToggleButton`、`ButtonLike`）都归入「Forms & Input」。

[crates/notifications/src/status_toast.rs:L149-L151](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/notifications/src/status_toast.rs#L149-L151) —— 跨 crate 的例子：notifications crate 的 `StatusToast` 同样只写 `ComponentScope::Notification`。scope 覆写与注册机制一样，对 crate 位置没有要求。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「改一行 `scope()`，组件在左侧导航里搬家」。

**操作步骤**（在你的本地克隆里做临时实验，做完还原）：

1. 运行 `cargo run -p component_preview --example component_preview`，在左侧导航确认 `Divider` 当前位于「Layout & Structure」分组。
2. 打开 `crates/ui/src/components/divider.rs`，把第 164 行的 `ComponentScope::Layout` 改成 `ComponentScope::Status`。
3. 重新运行 example，观察左侧导航。
4. 再把它改成 `ComponentScope::None`（或干脆注释掉整个 `fn scope()`，让它走默认值），重新运行。
5. 实验完毕，把代码还原。

**需要观察的现象**：

- 第 3 步后，`Divider` 从「Layout & Structure」消失，出现在「Status」分组里（`Indicator` 旁边）。
- 第 4 步后，「Layout & Structure」分组整体消失（如果 Divider 是该组唯一成员），`Divider` 出现在导航最底部的「Uncategorized」分组。
- 点开 Divider 详情页，头部小标签显示的是 **Unsorted** 而不是 Uncategorized（4.1.3 分析过的怪癖）。
- 在搜索框输入 `layout`，改前能命中 Divider（匹配 scope 显示名 `Layout & Structure`），改成 `Status` 后输入 `layout` 不再命中。

**预期结果**：分组归属、分组标题、分组排序、过滤命中、详情页标签，五处行为全部由 `scope()` 的返回值（及其 strum 显示名）驱动。以上现象为静态推演，具体表现待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：新组件 `Spinner` 是一个加载动画，应该选哪个 scope？如果暂时拿不准，最省事的做法是什么？

答案：`ComponentScope::Loading`（显示名 `Loading & Progress`）。拿不准时可以什么都不写——默认值 `None` 会把它放进尾部的「Uncategorized」分组，组件仍然可见、可搜索，之后再补归类也不迟。

**练习 2**：为什么「Forms & Input」分组排在「Editor」和「Images & Icons」之间，而不是按变体名 `Input` 排在 `Images` 之后？

答案：分组排序发生在 [component_preview.rs:L290](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L290)，`sort_by_key(|s| s.to_string())` 用的是 strum 序列化出的显示名。`Forms & Input` 以 F 开头，字典序落在 `Editor`（E）之后、`Images & Icons`（I）之前。

**练习 3**：左侧导航里未分类分组的标题是「Uncategorized」，但用过滤器输入 `uncategorized` 却未必能命中这些组件，为什么？

答案：过滤匹配走 [component_preview.rs:L202-L207](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L202-L207)，匹配的是组件名、scope 的 `to_string()`（对 `None` 来说是 `Unsorted`）与描述；「Uncategorized」这个标题是在 [component_preview.rs:L313](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L313) 硬编码的渲染文案，根本不参与匹配。想用过滤器找到未分类组件，输入 `unsorted` 才对。

### 4.2 ComponentStatus：四态生命周期与治理徽章

#### 4.2.1 概念说明

`ComponentStatus` 回答的问题是：「这个组件现在处于生命周期的哪个阶段，我能不能在正式功能里用它？」它是设计系统治理的最小载体——不是靠开会和对文档，而是把状态直接长在组件预览页的头部徽章上。

四个状态与治理语义：

| 状态 | strum 显示名 | 徽章颜色 | 语义 |
| --- | --- | --- | --- |
| `WorkInProgress` | `Work In Progress` | Warning（黄系） | 还在设计或只实现了一部分，**不应**在应用中使用 |
| `EngineeringReady` | `Ready To Build` | Info（蓝系） | 设计已完成或部分实现，等待工程师补完实现 |
| `Live` | `Live` | Success（绿系） | 可在应用中正常使用——**默认值，不渲染徽章** |
| `Deprecated` | `Deprecated` | Error（红系） | 不再推荐使用，未来版本可能移除 |

两个设计要点：

1. **`Live` 不显示徽章**。预览里绝大多数组件都是 `Live`，如果每个都挂一枚绿色徽章，真正的信号就被噪音淹没了。「默认态静默、异常态发声」是很值得借鉴的 UI 策略。
2. **状态词表刻意面向「设计 → 实现 → 上线 → 退役」的流程**：`EngineeringReady`（可以开工了）这种名字只有把设计和工程协作当成第一公民的团队才会造出来。

#### 4.2.2 核心流程

```text
组件作者覆写 status()              注册期                      预览期(单组件详情页头部)
──────────────────────────       ─────────────────────      ──────────────────────────────────
fn status() -> ComponentStatus    求值一次抄进               render_component_status()
  ComponentStatus::Deprecated ──► ComponentMetadata.status ──► status == Live?
                                     (之后不再回调 trait)          ├─ 是 → 返回 None,不渲染
                                                                  └─ 否 → 渲染徽章:
                                                                       颜色 = 四态映射
                                                                       文案 = status.to_string()
                                                                       tooltip = status.description()
```

徽章的渲染规则：

1. 颜色由四态到语义色（`Color::Error/Info/Success/Warning`）的映射决定，背景色是同色 12% 透明度的浅色块。
2. 文案是 strum 显示名，如 `Work In Progress`。
3. tooltip 完整展示该状态的治理文案（`status.description()`），悬停即读。
4. 徽章本身是 `disabled(true)` 的 `ButtonLike`——外观像按钮，但不响应点击，纯展示。

#### 4.2.3 源码精读

[crates/component/src/component.rs:L268-L276](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L268-L276) —— `ComponentStatus` 枚举定义。注意 `WorkInProgress` 与 `EngineeringReady` 各挂了 `#[strum(serialize = ...)]` 属性——Rust 变体名习惯驼峰（`WorkInProgress`），而徽章文案要给人看（`Work In Progress`），中间靠 strum 转换。`Live` 与 `Deprecated` 单词本就是单词，无需属性。

[crates/component/src/component.rs:L193-L195](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L193-L195) —— `status()` 默认返回 `Live`。与 `scope()` 的默认值 `None`（进入特殊分组）对照着记：scope 的默认是「待归类」，status 的默认是「健康常态」。

[crates/component/src/component.rs:L278-L296](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L278-L296) —— `ComponentStatus::description()`，四段治理文案的原文。这段文案会原样成为徽章的 tooltip，例如 `WorkInProgress` 对应 "These components are still being designed or refined. They shouldn't be used in the app yet."。注意一个贴心的细节：文案全用「These components ...」的复数句式，因为同一段文案也适合整体描述某类状态。

[crates/component_preview/src/component_preview.rs:L904-L939](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L904-L939) —— `render_component_status`，徽章的全部渲染逻辑：四态到颜色的 match 映射（第 912-917 行）；`if status != ComponentStatus::Live` 的静默规则（第 919 行）；`ButtonLike` + `color.color(cx).alpha(0.12)` 浅色背景 + `Label::new(status.to_string())` 文案 + `Tooltip::text(status_description)` 悬停说明 + `disabled(true)`（第 920-935 行）。函数签名返回 `Option<impl IntoElement>`，`None` 直接不给徽章留位置。

[crates/component_preview/src/component_preview.rs:L958-L966](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L958-L966) —— 徽章的挂载点：详情页头部，组件大标题（`Headline`）右侧，通过 `.children(self.render_component_status(cx))` 插入——`Option` 为 `None` 时 `children` 什么都不加，标题独占一行。

[crates/ui/src/components/notification/alert_modal.rs:L168-L175](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/notification/alert_modal.rs#L168-L175) —— 目前整个组件树里**唯一**覆写 `status()` 的组件：`AlertModal` 同时声明 `ComponentScope::Notification` 与 `ComponentStatus::WorkInProgress`。想在真实预览里看非 `Live` 徽章长什么样，打开组件预览搜 `AlertModal` 即可——黄色系「Work In Progress」徽章。

补充一个导入层面的细节：`ComponentStatus` 并不在 `ui::prelude` 里（[crates/ui/src/prelude.rs:L11](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/prelude.rs#L11) 只导出了 `Component, ComponentScope, ...`），它住在面向组件作者的 [crates/ui/src/component_prelude.rs:L1-L4](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/component_prelude.rs#L1-L4)。这正对应 u1-l3 确立的分界：普通写 UI 的人不需要关心组件生命周期标记。

#### 4.2.4 代码实践

**实践目标**：亲手让同一枚徽章在三种非 `Live` 状态间切换，记录颜色、文案、tooltip 的对应关系。

**操作步骤**（本地临时实验，做完还原）：

1. 先看真实样本：运行组件预览，搜索并打开 `AlertModal`，记录头部徽章的外观与悬停文案。
2. 打开 `crates/ui/src/components/divider.rs`，在 `impl Component for Divider` 块里、`fn scope()` 之前加一段：

   ```rust
   // 示例代码：本地实验用，观察完请删除
   fn status() -> ComponentStatus {
       ComponentStatus::Deprecated
   }
   ```

3. 由于 divider.rs 只 `use crate::prelude::*` 而 `ComponentStatus` 不在其中，还需要在该文件顶部补一行 `use crate::component_prelude::ComponentStatus;`（这正是 4.2.3 末尾那个细节的现身说法）。
4. 重新运行预览，打开 `Divider` 详情页，记录徽章颜色与文案，悬停记录 tooltip。
5. 把 `Deprecated` 依次换成 `WorkInProgress`、`EngineeringReady`，重复观察；最后换回 `Live`（或删除这段覆写），确认徽章彻底消失。
6. 还原全部改动。

**需要观察的现象**：

- `Deprecated`：红系配色，文案 `Deprecated`，tooltip 为「...no longer recommended for use in the app, and may be removed in a future release.」
- `WorkInProgress`：黄系配色，文案 `Work In Progress`（三个词、带空格），tooltip 为「...still being designed or refined...」
- `EngineeringReady`：蓝系配色，文案是 `Ready To Build` 而**不是**变体名 `EngineeringReady`。
- `Live`：头部只剩标题，徽章位置完全收起。

**预期结果**：四态 → 三色（Live 无色）→ 四段 tooltip 一一对应，全部由 `render_component_status` 与 `ComponentStatus::description` 两处源码决定。以上为源码推演，具体色值随主题（亮/暗）变化，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Live` 状态不渲染徽章？如果产品经理坚持要求 Live 也显示绿色徽章，至少要动哪两处源码？

答案：默认态静默是为了信号/噪音比——绝大多数组件是 Live，全员挂绿徽章会淹没真正需要关注的 WIP/Deprecated 信息。改动点：一是 [component_preview.rs:L919](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L919) 的 `if status != ComponentStatus::Live` 条件（改为恒真或删除 else 分支）；二是函数 doc 注释（第 904-907 行）里「Live 不渲染」的说明，保持文档与行为一致。

**练习 2**：徽章显示 `Ready To Build`，但 Rust 里找不到 `ReadyToBuild` 这个变体，这是怎么回事？

答案：变体名是 `EngineeringReady`，`Ready To Build` 是它挂的 `#[strum(serialize = "Ready To Build")]` 属性给出的显示名（[component.rs:L272-L273](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L272-L273)）。徽章文案来自 `Label::new(status.to_string())`，走的是 strum `Display` 派生。

**练习 3**：组件树里为什么几乎找不到 `status()` 的覆写？

答案：因为默认值 `Live` 恰是常态，健康组件无需声明；只有处于特殊生命周期的组件（目前仅 `AlertModal` 标了 `WorkInProgress`）才需要显式标记。这是「默认值应取最常见状态」的又一直接收益。

### 4.3 strum 的 Display 与 EnumString：一份枚举定义，两个方向

#### 4.3.1 概念说明

strum 让枚举的「人与机器」两个方向各有一个派生：

- **`Display`**（枚举 → 字符串）：为枚举实现 `std::fmt::Display`，于是 `scope.to_string()` 可用。这是**展示方向**，component_preview 里到处在用。
- **`EnumString`**（字符串 → 枚举）：为枚举实现 `FromStr`，于是 `"Forms & Input".parse::<ComponentScope>()` 可用。这是**解析方向**。

`#[strum(serialize = "...")]` 属性同时作用于两个方向：`Input.to_string()` 得到 `"Forms & Input"`，而 `"Forms & Input".parse::<ComponentScope>()` 得到 `Ok(ComponentScope::Input)`。一份属性，两个方向共享同一张映射表。

为什么这很重要？因为显示名一旦参与排序（分组序）、过滤（搜索命中）和标签渲染，它就成了事实上的「对外契约」——改一个 `serialize` 字符串，用户的搜索习惯和导航顺序都会跟着变。`Display` + `EnumString` 成对出现，意味着这张契约表是可逆的：字符串可以无歧义地解析回枚举。

#### 4.3.2 核心流程

```text
                    #[strum(serialize = "Forms & Input")]
                              Input,
                                │
              ┌─────────────────┴──────────────────┐
        派生 Display                          派生 EnumString
              │                                      │
  ComponentScope::Input.to_string()      "Forms & Input".parse::<ComponentScope>()
  == "Forms & Input"                     == Ok(ComponentScope::Input)
              │                                      │
      展示方向(预览在用):                    解析方向(树内暂无消费者):
      · 分组标题 SectionHeader             · 可用于测试断言
      · 分组排序 sort_by_key               · 可用于未来的配置/持久化
      · 过滤匹配 scope_name
      · 详情页头部小标签
      · status 徽章文案
```

#### 4.3.3 源码精读

[crates/component/src/component.rs:L19](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L19) —— 一行 `use strum::{Display, EnumString};`，两个派生的全部引入。

[crates/component/src/component.rs:L268](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L268) 与 [crates/component/src/component.rs:L298](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L298) —— 两个枚举的 derive 列表都以 `Display, EnumString` 收尾，且都额外派生了 `Debug, Clone, PartialEq, Eq, Hash`（后者要当 HashMap 的键，4.1 已见）。

展示方向的五个真实消费点（前面两节都已精读过，这里汇总）：

| 消费点 | 位置 | 用的字符串 |
| --- | --- | --- |
| 分组标题 | [component_preview.rs:L297](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L297) | `scope.to_string()` → 导航分组标题 |
| 分组排序 | [component_preview.rs:L290](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L290) | 按显示名字典序 |
| 过滤匹配 | [component_preview.rs:L202](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L202) | scope 显示名的小写包含匹配 |
| 详情页标签 | [component_preview.rs:L954](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L954) | 头部小号弱色标签 |
| 状态徽章文案 | [component_preview.rs:L928](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L928) | `status.to_string()` → 如 `Work In Progress` |

解析方向的现状要诚实地说明：**目前整个 Zed 组件树里没有任何代码调用 `ComponentScope::from_str` 或 `parse::<ComponentStatus>()`**（可用 grep 验证）。`EnumString` 在这里是一份「能力储备」——它保证了显示名这张契约表是可逆的，测试可以直接用字符串断言/构造枚举，未来若要把 scope/status 写进配置文件或 SQLite（比如 u4-l3 的持久化扩展），解析方向已经就绪，不必再补派生。

#### 4.3.4 代码实践

**实践目标**：验证 strum 的往返性质（roundtrip）：`to_string()` 与 `parse()` 互为逆操作。

**操作步骤**：

1. 在 component crate 下新建临时测试（可加在 `crates/component/src/component.rs` 文件末尾，实验后删除）：

   ```rust
   // 示例代码：本地实验用，验证后请删除
   #[test]
   fn scope_display_roundtrip() {
       let scope = ComponentScope::Input;
       let as_text = scope.to_string();
       assert_eq!(as_text, "Forms & Input");
       let parsed: ComponentScope = as_text.parse().unwrap();
       assert_eq!(parsed, scope);

       let status = ComponentStatus::EngineeringReady;
       assert_eq!(status.to_string(), "Ready To Build");
       let parsed: ComponentStatus = "Ready To Build".parse().unwrap();
       assert_eq!(parsed, status);
   }
   ```

2. 运行 `cargo test -p component scope_display_roundtrip`。
3. 再补一个反向断言试试「非契约字符串」的行为：`assert!("forms&input".parse::<ComponentScope>().is_err());`（注意没有空格、大小写也不同），观察 `EnumString` 的默认匹配是大小写敏感的精确匹配。
4. 实验完毕删除测试代码。

**需要观察的现象**：往返断言通过；大小写或空格不精确的字符串解析失败（返回 `Err`）。

**预期结果**：`Display` 与 `EnumString` 共享 `#[strum(serialize = ...)]` 这一张映射表，且解析方向是严格匹配。第 3 步的报错类型待本地验证（strum 默认返回 `ParseError` 变体）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `Input` 的 serialize 属性从 `"Forms & Input"` 改成 `"Form Controls"`，会连带影响预览界面的哪几处行为？

答案：至少四处——(1) 导航分组标题由 `Forms & Input` 变为 `Form Controls`；(2) 该分组在导航中的字典序位置移动（F 不变、但后续字符变了，`Form Controls` 与 `Forms & Input` 的排序位置可能不同）；(3) 过滤行为变化：输入 `forms` 不再命中该组组件（`form` 仍可命中）；(4) 组件详情页头部的 scope 小标签文本变化。这解释了为什么说显示名是「对外契约」。

**练习 2**：`EnumString` 在树内没有消费者，为什么还值得派生？

答案：它把「显示名 ↔ 枚举」的映射补全为双射，成本低（一行派生），收益是：测试可以直接以字符串书写期望值；未来做配置化、持久化（如把 scope 存库再读回）时无需补派生或手写 match。派生宏的「提前储备」在契约表这种中心数据上尤其划算。

**练习 3**：`ComponentScope` 的 derive 里有 `Hash`，`ComponentStatus` 也有，两者都必要吗？

答案：`ComponentScope` 的 `Hash + Eq` 是硬需求——它是 `scope_ordered_entries` 里 `HashMap<ComponentScope, ...>` 分组容器的键。`ComponentStatus` 目前没有做键的用法，属于与兄弟枚举保持一致的储备（`ComponentMetadata` 需要 `Clone`，两个枚举都需 `Clone` 支持 `scope()`/`status()` 的按值返回）。判断派生是否「必要」，要以真实消费点为准。

## 5. 综合实践

**任务**：为你自己的练习组件（可以仿照 u2-l1 实践中的最小组件，或直接借用 `Divider`）完成一次「分类 + 生命周期」双标注，并跑通验证闭环。

1. **选定组件与作用域**：为组件实现 `fn scope()`，从 16 个真实分类里挑一个语义最贴切的（例如做搜索框组件选 `Input`，做骨架屏选 `Loading`）。运行 `cargo run -p component_preview --example component_preview`，确认它出现在预期分组、组内按 `sort_name`（或默认 `name`）排序的位置合理。
2. **标注生命周期**：为它实现 `fn status()`（记得 `ComponentStatus` 要从 `ui::component_prelude` 或 component crate 导入）。先标 `WorkInProgress` 运行一次，再改成 `Deprecated` 运行一次，逐项记录：徽章颜色、徽章文案、tooltip 全文，与 4.2 表格逐格对照。
3. **验证契约表**：按 4.3.4 的写法补一个往返测试，断言你选的 scope 的 `to_string()` 与 `parse()` 互逆。
4. **故意归错类**：最后把 scope 改成 `None` 再运行一次，观察组件落入导航底部「Uncategorized」、详情页头部显示「Unsorted」的现象，把三处表现（导航分组、总览页无后缀、详情页 Unsorted 标签）截图或记录成笔记。
5. **清理**：还原所有源码改动，只留下你的观察记录。

这个任务串起了本讲的三个最小模块：scope 的分组与排序（4.1）、status 的徽章与文案（4.2）、strum 两个方向共享的契约表（4.3）。

## 6. 本讲小结

- `ComponentScope` 共 17 个变体：16 个真实分类 + 1 个 `None` 哨兵（默认值，strum 显示名 `Unsorted`）；未分类组件在导航中被排除出字典序、归入硬编码标题「Uncategorized」的尾部分组。
- 分组标题、分组字典序、过滤命中、详情页标签都消费 strum **显示名**（如 `Input` → `Forms & Input`），显示名因此成为对外契约。
- `ComponentStatus` 是四态生命周期：`WorkInProgress`（黄/Warning）、`EngineeringReady`（蓝/Info）、`Live`（默认，绿/Success 但**不渲染徽章**）、`Deprecated`（红/Error）；tooltip 文案来自 `ComponentStatus::description()` 的四段治理文案。
- 目前组件树里唯一覆写 `status()` 的是 `AlertModal`（`WorkInProgress`）；`ComponentStatus` 只在 `ui::component_prelude` 导出，不在 `ui::prelude`。
- strum 的 `Display`（展示方向）被预览界面五处消费；`EnumString`（解析方向）目前树内无消费者，是保证契约表可逆的能力储备。

## 7. 下一步学习建议

本讲讲完了 `Component` trait 七个方法背后的最后两个枚举，第二单元的注册机制与核心数据结构到此收束。接下来进入第三单元：

- **u3-l1**（`ComponentExample` 与 `ComponentExampleGroup` 布局元素）：scope 决定组件「放在哪个货架」，而这两个布局元素决定每个示例卡片「长什么样」。
- **u3-l2**（布局辅助函数与高质量 preview 的写法）：学习 `single_example`、`example_group_with_title` 等助手，它们是你为自己的 scope/status 标注组件写 `preview()` 的直接材料。
- 若想先看消费端的其余部分，可跳读 **u4-l2**（过滤、作用域分组与高亮列表）——本讲 4.1.3 精读的 `scope_ordered_entries` 在那一讲会有更完整的逐行拆解（含 `is_char_boundary` 的高亮处理）。
