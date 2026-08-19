# inventory 分布式注册：ComponentFn 与 init()

## 1. 本讲目标

上一讲（u2-l2）我们看到：`component::init()` 执行后，全局注册表 `COMPONENT_DATA` 里就有了几十个组件的元数据。但有一个关键问题被刻意跳过了——**这些注册动作是从哪里来的的？没有任何人手工调用过 `register_component::<Divider>()`，是谁替我们调用的？**

本讲回答这个问题。学完后你应该能够：

1. 清晰区分「登记」与「执行」两个阶段：登记发生在程序装载期（`main` 之前），执行发生在 `component::init()` 被调用时。
2. 说出 `inventory::collect!` / `submit!` / `iter` 三个宏各自的角色，以及「只有被链接进二进制的 crate 才会贡献登记项」这条铁律。
3. 解释 `component::__private::inventory` 这个看起来别扭的路径解决的是哪两个问题（依赖可见性 + 版本同一性），并知道它是仓库内至少五套机制共用的惯用法。
4. 脱离派生宏，手写一条完整的注册链路。

## 2. 前置知识

本讲不涉及新的 UI 知识，但涉及几个 Rust 工具链层面的概念，先用通俗语言补齐。

### 2.1 `main` 之前发生了什么

一个程序从「双击运行」到「进入 `main` 函数」之间并非空白。以 Linux 的 ELF 可执行文件为例，链接器会把一些「启动函数」放进 `.init_array` 这样的特殊段里，动态加载器在跳转到 `main` 之前会先逐个调用它们。C 语言的 constructor attribute、Rust 生态里各种「ctor 式」技巧，都是往这类段里塞代码。

`inventory` crate 正是利用这一点：它让每个 `submit!` 宏展开出一个静态节点和一小段「在 `main` 之前运行」的挂载代码，把节点挂进全局集合。所以等你「进到」 `main` 时，集合已经装好了。这是理解本讲的第一把钥匙：**收集不需要任何人在运行期显式触发，它是链接器布局的副产品。**

（背景知识：`inventory` 0.3 在默认 stable 实现里走上述 ctor 式路线，其 `experimental` 特性改用链接器自定义段。实现细节以 inventory 官方文档为准，本讲只依赖其可观察的行为语义。）

### 2.2 声明宏、过程宏与「宏卫生」

- 声明宏（`macro_rules!`）在展开时可以用 `$crate` 指代「定义宏的那个 crate」，保证展开结果里的路径永远指回宏的属主。
- 过程宏（如 `#[derive(RegisterComponent)]`）没有 `$crate` 等价物。它运行在 `ui_macros` 这个独立的编译单元里，只能**生成一段文本**（token 流），这段文本随后在**使用宏的 crate**（比如 `ui`）里编译。因此宏生成的代码里只能写「使用方一定能解析的绝对路径」，例如 `component::xxx`。
- 「宏卫生」问题在这里具体表现为：宏展开引用的每个路径，都必须在使用方的依赖图里可解析，而且必须指向正确的那个 crate 实例。第 4.3 节会看到 `__private` 模块如何解决它。

### 2.3 依赖 = 链接包含性

Cargo 的依赖声明最终决定「哪些代码进入二进制」。一个 crate 就算参与编译，如果没有被最终二进制的依赖图（直接或传递）引用，链接器可以整体丢弃它——它里面的静态节点、启动代码也一并消失，**没有任何警告**。这条规则是理解 `inventory` 各种「为什么没注册上」疑惑的根源。

### 2.4 承接前两讲的既有结论

- `Component` trait 的七个方法都是无 `self` 的关联函数（u2-l1），因此注册动作可以浓缩为一个普通函数调用 `register_component::<T>()`。
- `register_component` 把 trait 信息抄录成 `ComponentMetadata` 值快照，以 id 为键插入 `COMPONENT_DATA: LazyLock<RwLock<ComponentRegistry>>`（u2-l2）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `crates/component/src/component.rs` | 机制属主：`ComponentFn` 新类型、`inventory::collect!`、`init()`、`__private` 模块、`register_component` |
| `crates/ui_macros/src/derive_register_component.rs` | `#[derive(RegisterComponent)]` 的展开模板，全树绝大多数 `submit!` 的来源 |
| `crates/ui/src/components/button/toggle_button.rs` | 手写注册的真实范例（泛型组件绕开派生宏） |
| `crates/ui_macros/Cargo.toml` / `crates/ui/Cargo.toml` | 依赖证据：前者不依赖 `component`，后者不依赖 `inventory` |
| `crates/workspace/src/workspace.rs` / `crates/component_preview/examples/component_preview.rs` | `init()` 的两个真实调用点 |
| `crates/gpui/src/gpui.rs`、`crates/gpui_macros/src/register_action.rs`、`crates/feature_flags/src/store.rs` | 仓库内使用同一手法的平行案例（actions、feature flags） |

## 4. 核心概念与源码讲解

### 4.1 ComponentFn：把「注册动作」变成可收集的数据

#### 4.1.1 概念说明

`register_component::<T>()` 是一个普通的泛型函数，调用它就「执行一次注册」。但 `inventory` 收集的是**值**（数据），不是代码。所以需要一个容器，把「将来要执行的注册」打包成一个可以放进静态内存、可以在集合里被枚举的值——这就是 `ComponentFn`：

```rust
pub struct ComponentFn(fn());
```

它是对裸函数指针 `fn()` 的 newtype 包装。为什么非要包一层？

1. `inventory::collect!` 以**类型**为单位建立集合，给被收集的东西一个专属类型名，让 `collect!`/`submit!`/`iter::<T>` 三方有共同的锚点。直接拿 `fn()` 这种函数指针类型当集合类型虽然语法上可行，但可读性和诊断信息都很差。
2. `ComponentFn::new` 是 `const fn`，允许在 `submit!` 展开出的静态上下文里构造这个值。
3. newtype 留出了演化空间——将来要附带额外信息时加字段即可，不破坏调用方。

由此得到本讲的核心心智模型——**两阶段**：

- **阶段一（登记）**：各个 crate 在自己的代码里提交 `ComponentFn` 值。登记的内容只是「一个待执行的函数指针」，此时什么注册都没发生，`COMPONENT_DATA` 仍是空的。
- **阶段二（执行）**：`component::init()` 遍历集合，逐个调用函数指针，才真正触发 `register_component::<T>()` 写入注册表。

为什么要把执行推迟到 `init()`，而不是让挂载代码在 `main` 之前直接注册？可以观察到这样三个实际收益：

1. **时机确定**：注册统一发生在框架初始化处（`workspace::init` 的第一行），而不是散落在链接器决定的启动顺序里。
2. **幂等无害**：`init()` 调用两次只是对同一个 id 再 `insert` 一次，值不变，因此 example 和 workspace 各调一次也不出错。
3. **快照语义有明确起点**：`components()` 返回整表克隆（u2-l2），有了「init 之前必然为空、init 之后基本不再变化」的约定，读快照的消费者才敢缓存。

#### 4.1.2 核心流程

```text
编译期    每处 submit! 展开为：静态节点 + main 前挂载代码（留在所在 crate 的目标文件里）
链接期    链接器决定哪些 crate 进入二进制；未链接的 crate 的节点随之消失（无警告）
装载期    main 之前，挂载代码把节点逐个挂进 ComponentFn 集合 —— 登记完成
───────────────────────────────────────────────────────────── 以下为运行期
init 期   component::init() → inventory::iter::<ComponentFn>() 枚举集合
          → (f.0)() 调用每个函数指针
          → register_component::<T>() 求值 trait 关联函数、构造 ComponentMetadata
          → COMPONENT_DATA.write().components.insert(id, metadata)
使用期    components() 拿读锁克隆快照 → component_preview 渲染
```

注意分界线的位置：`init()` **不负责收集**。它开始工作时，收集早已完成；它只负责把「待办清单」执行掉。

#### 4.1.3 源码精读

先看被收集的类型与它的构造函数——[crates/component/src/component.rs:31-37](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L31-L37)：

```rust
pub struct ComponentFn(fn());

impl ComponentFn {
    pub const fn new(f: fn()) -> Self {
        Self(f)
    }
}
```

定义「执行阶段」入口的 [crates/component/src/component.rs:25-29](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L25-L29)：

```rust
pub fn init() {
    for f in inventory::iter::<ComponentFn>() {
        (f.0)();
    }
}
```

`inventory::iter::<ComponentFn>()` 枚举全程序所有已提交的 `ComponentFn`，`(f.0)()` 解出内部的函数指针并调用。整个仓库里 `inventory::iter::<ComponentFn>` 只有这一处消费点——阶段二的入口是唯一且集中的。

再看执行阶段的终点（u2-l2 已精读过，这里只标注它在链路中的位置）——[crates/component/src/component.rs:47-61](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L47-L61)：`register_component::<T>()` 求值 `T` 的七个关联函数、固化成 `ComponentMetadata`、拿写锁插入 `COMPONENT_DATA`。

手写登记的真实范例在 [crates/ui/src/components/button/toggle_button.rs:404-410](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L404-L410)：

```rust
fn register_toggle_button_group() {
    component::register_component::<ToggleButtonGroup<ToggleButtonSimple>>();
}

component::__private::inventory::submit! {
    component::ComponentFn::new(register_toggle_button_group)
}
```

这段代码完整展示了「不用派生宏时的手写三件套」：一个无参注册函数、一次 `ComponentFn::new` 包装、一次 `submit!` 提交。

`ToggleButtonGroup` 为什么必须手写？看它的定义——[crates/ui/src/components/button/toggle_button.rs:171](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L171)：

```rust
pub struct ToggleButtonGroup<T, const COLS: usize = 3, const ROWS: usize = 1>
```

以及 `Component` 的实现——[crates/ui/src/components/button/toggle_button.rs:412-414](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L412-L414)：

```rust
impl<T: ButtonBuilder, const COLS: usize, const ROWS: usize> Component
    for ToggleButtonGroup<T, COLS, ROWS>
```

派生宏只会生成 `register_component::<ToggleButtonGroup>` 这样不带泛型参数的调用（见 4.3.3 的展开模板），而裸的 `ToggleButtonGroup` 是未具体化的泛型类型，根本没有实现 `Component`。所以作者手写注册并**钉死一个具体实例** `ToggleButtonGroup<ToggleButtonSimple>`，让类型检查得以通过。这是「泛型组件绕开派生宏」的标准姿势，也是 `ComponentFn` 手写通道存在的现实理由。

最后确认阶段二的两个触发点：框架侧，[crates/workspace/src/workspace.rs:784-785](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/workspace/src/workspace.rs#L784-L785) 中 `workspace::init` 的第一行就是 `component::init()`；独立运行侧，[crates/component_preview/examples/component_preview.rs:31-32](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L31-L32) 中 example 的 `main` 在 `application().run(...)` 闭包开头也调了一次。两处调用印证了「幂等无害」的设计。

#### 4.1.4 代码实践：跟踪一条完整调用链（源码阅读型）

1. **实践目标**：不运行任何代码，仅凭阅读，写出从 `workspace::init` 到 `COMPONENT_DATA` 插入的完整调用链。
2. **操作步骤**：
   - 打开 `crates/workspace/src/workspace.rs` 第 785 行，确认 `component::init()` 是 `workspace::init` 函数体的第一条语句。
   - 打开 `crates/component/src/component.rs` 第 25-29 行，记下 `for f in inventory::iter::<ComponentFn>()` 与 `(f.0)()`。
   - 打开 `crates/ui_macros/src/derive_register_component.rs` 第 19-26 行，找到 `f.0` 指向的函数长什么样（`__component_registry_internal_register_*` 内部调用 `component::register_component::<#name>()`）。
   - 回到 `component.rs` 第 47-61 行，确认 `register_component` 以 `data.components.insert(id, metadata)` 收尾。
3. **需要观察的现象**：链路中没有任何一处「中央清单」列出全部组件；每一环都只认识下一环的抽象形态（函数指针 / trait 约束）。
4. **预期结果**：得到如下链条（可对照检查）：

   ```text
   workspace::init  (workspace.rs:785)
     └─ component::init()                       (component.rs:25)
         └─ inventory::iter::<ComponentFn>()    枚举装载期挂好的集合
             └─ (f.0)()                          即 __component_registry_internal_register_Divider 一类函数
                 └─ component::register_component::<Divider>()   (component.rs:47)
                     └─ COMPONENT_DATA.write().components.insert(id, metadata)
   ```

   反向思考题：若删掉两处 `init()` 调用，集合里仍有节点，但没人执行，`components()` 会返回空注册表、预览面板没有任何组件（此结论与 u1-l2 的注释实验一致；具体现象待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`ComponentFn` 里的字段是 `fn()`（裸函数指针），为什么不用 `Box<dyn Fn()>` 或者直接存 `ComponentMetadata`？

**参考答案**：`submit!` 展开出的值要放进静态内存，必须是编译期可确定、无堆分配、`Sync` 的数据；`fn()` 指针满足全部条件，而 `Box<dyn Fn()>` 不满足。也不能直接存 `ComponentMetadata`——构造元数据要求值 `T::description()` 等关联函数，而 `T` 的具体类型只有在泛型函数 `register_component::<T>` 内部才可见；`submit!` 处拿不到「值形态」的类型信息，只能存下「稍后替我执行」的函数指针，让泛型代码在函数体里展开。

**练习 2**：`init()` 被调用两次会发生什么？为什么是安全的？

**参考答案**：集合被再遍历一遍，每个组件的 `register_component` 再执行一次，以相同 id 向 `HashMap` `insert` 相同内容，等于覆盖写，结果不变。安全性来自「id 确定且元数据是纯函数求值」——没有计数器、追加列表这类非幂等操作。

**练习 3**：为 `ToggleButtonGroup` 补一个 `#[derive(RegisterComponent)]` 为什么编译不过？

**参考答案**：派生宏会生成 `component::register_component::<ToggleButtonGroup>()` 与编译期断言 `AssertComponent<ToggleButtonGroup>`。裸 `ToggleButtonGroup` 是带 `T: ButtonBuilder` 和两个 const 泛型参数的类型构造器，未具体化，不满足 `T: Component`，断言与调用都无法通过编译。手写注册钉死 `ToggleButtonGroup<ToggleButtonSimple>` 这个具体实例才能通过。

### 4.2 inventory 的 collect! 与 submit!：分布式登记箱

#### 4.2.1 概念说明

`inventory` 解决的是「插件式注册」问题：N 个 crate 各自登记一点东西，不需要任何中央清单文件，也不需要在启动时手工调用 N 个注册函数。整个机制由三个角色构成：

| 宏 / 函数 | 谁来用 | 用几次 | 作用 |
| --- | --- | --- | --- |
| `inventory::collect!(T)` | 类型 `T` 的属主 crate（这里是 `component`） | 每个类型一次 | 建立并声明 `T` 的集合 |
| `inventory::submit!(value)` | 任意依赖属主 crate 的 crate | 每次登记一次 | 提交一个 `T` 类型的值 |
| `inventory::iter::<T>()` | 任意运行期代码 | 任意次 | 枚举全程序提交的所有值 |

名字里的「分布式」体现在：集合没有中心化的注册点，节点分散在各个 crate 的目标文件里，由链接器在构建二进制时汇聚。`component.rs` 顶部模块文档也把它称为 "the distributed slice mechanism for registering components"（[crates/component/src/component.rs:1-8](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L1-L8)）。

版本方面：仓库根 `Cargo.toml` 锁定 [inventory = "0.3.19"](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/Cargo.toml#L663)，`component` crate 经 workspace 继承（[crates/component/Cargo.toml:17](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/Cargo.toml#L17)）。

#### 4.2.2 核心流程

把「哪些值会被枚举到」写成公式：

\[ \mathrm{iter}(\text{ComponentFn}) \;=\; \bigcup_{c \,\in\, \mathrm{linked}(B)} \mathrm{submits}(c, \text{ComponentFn}) \]

其中 \(\mathrm{linked}(B)\) 表示「被链接进二进制 \(B\) 的全部 crate」。这个集合是公式的灵魂：**不在依赖图里的 crate，一个节点也不会贡献**。

把两个阶段按时间轴展开：

```text
cargo build
  ├─ ui crate 编译：derive 展开 → submit! 展开 → 静态节点进入 ui 的目标文件
  ├─ settings_ui crate 编译：同上
  └─ 链接 component_preview 二进制：把依赖图里所有 crate 的目标文件并入
进程启动（main 之前）
  └─ 各节点的挂载代码运行 → ComponentFn 集合装配完成
main / run 闭包
  └─ component::init() 枚举集合并执行注册
```

#### 4.2.3 源码精读

集合的声明只有一行——[crates/component/src/component.rs:39](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L39)：

```rust
inventory::collect!(ComponentFn);
```

它必须写在 `component` crate 里（`ComponentFn` 的属主），且全树只此一处——集合与类型一一对应。

消费点在 [crates/component/src/component.rs:26](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L26)（`init()` 内，见 4.1.3）。

登记的两种来源：

- **派生宏生成**（绝大多数组件走这条路）——[crates/ui_macros/src/derive_register_component.rs:19-26](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L19-L26)：

  ```rust
  #[allow(non_snake_case)]
  fn #register_fn_name() {
      component::register_component::<#name>();
  }

  component::__private::inventory::submit! {
      component::ComponentFn::new(#register_fn_name)
  }
  ```

  `#name` 是被 derive 的类型名，`#register_fn_name` 是 `__component_registry_internal_register_{name}`（拼接逻辑见 [crates/ui_macros/src/derive_register_component.rs:9-12](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L9-L12)）。`#[allow(non_snake_case)]` 是因为函数名里嵌入了 CamelCase 的类型名。展开的完整三段结构（编译期断言 + 注册函数 + submit）留待 u2-l4 逐行精读，本节只需盯住「submit 提交了一个 ComponentFn」这一段。

- **手写**（泛型组件等特殊场景）——即 4.1.3 引用过的 [crates/ui/src/components/button/toggle_button.rs:408-410](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/toggle_button.rs#L408-L410)。

同一个 `inventory` 在仓库里还支撑着其他机制，可作为交叉验证的阅读材料：gpui 的 action 注册走 [crates/gpui_macros/src/register_action.rs:41-43](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_macros/src/register_action.rs#L41-L43) 的 `gpui::private::inventory::submit!`；feature flags 走 [crates/feature_flags/src/store.rs:37-41](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/feature_flags/src/store.rs#L37-L41)；settings、db 也各有消费点。它们与本讲机制的形态完全同构。

#### 4.2.4 代码实践：验证「链接包含性」

1. **实践目标**：亲眼确认「预览里出现的组件 = 被链接进 example 二进制的 crate 所提交的组件」。
2. **操作步骤**：
   - 阅读 [crates/component_preview/Cargo.toml:18-40](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/Cargo.toml#L18-L40)，列出 `[dependencies]`：其中 `component`、`ui`、`ui_input`、`workspace` 等直接依赖都会把自身及传递依赖带进二进制。
   - 运行 `cargo run -p component_preview --example component_preview`，打开预览，在左侧导航按分组清点组件来自哪些 crate（`ui::Divider` 一类的完整类型路径可以在过滤框里搜出来）。
   - 思考实验（纸面推演即可）：新建一个只依赖 `component`、不依赖 `ui` 的最小二进制，在 `main` 里调用 `component::init()` 后打印 `component::components().len()`。
3. **需要观察的现象**：example 的依赖图决定了可见组件的范围；`ui` 之外的组件（如 `settings_ui` 里的 `NumberField`）能否出现，取决于它是否在 example 的传递依赖链上。
4. **预期结果**：思考实验中打印值为 `0`——没有任何组件 crate 被链接进来，`ComponentFn` 集合为空，`init()` 遍历空集什么都不做。这正是第 2.3 节「依赖 = 链接包含性」的直接推论（待本地验证；生产平台上构建一次最小 gpui 工程即可复现）。

#### 4.2.5 小练习与答案

**练习 1**：把 `inventory::collect!(ComponentFn)` 从 `component` crate 挪到 `ui` crate 里会怎样？

**参考答案**：不可行也无意义。`collect!` 为类型建立全局唯一的集合，应当在类型属主 crate 声明；挪到 `ui` 后 `component::init()` 里的 `inventory::iter::<ComponentFn>` 仍指向 `component` 所依赖的那个 inventory 实例中为 `ComponentFn` 建立的集合，而 `ui` 里的 `collect!` 与 `ui_macros` 生成的 submit 会落到另一个签名不匹配的语境，轻则编译失败，重则提交与遍历各走各的集合、组件凭空消失。属主关系不要拆散。

**练习 2**：一个 crate 写了 `submit!` 但没被最终二进制依赖，会发生什么？

**参考答案**：整个 crate 不进链接，静态节点和挂载代码一起消失，无编译错误、无运行警告，组件就是不出现。排查「组件没注册上」时第一件事就是检查该 crate 是否真的被二进制依赖。

**练习 3**：`inventory::iter` 枚举的顺序有保证吗？对注册表有影响吗？

**参考答案**：没有保证。仓库里有现成的注释佐证——[crates/settings_ui/src/pages/feature_flags.rs:15](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/settings_ui/src/pages/feature_flags.rs#L15) 明确写着 "`inventory::iter` order depends on link order"。对注册表无影响：`COMPONENT_DATA` 内部是 `HashMap<ComponentId, ComponentMetadata>`，写入顺序无关；展示层的顺序由 `sorted_components`（按 `name`）与预览层的 `sort_name` 分组排序决定，即 u2-l2 讲过的「两层排序」。

### 4.3 `__private` 模块：宏展开的「依赖摆渡」

#### 4.3.1 概念说明

回看派生宏生成的提交代码：`component::__private::inventory::submit! {...}`。为什么不直接写 `inventory::submit!`？这个绕行解决两个实际问题。

**问题一：依赖可见性（宏卫生）。** `submit!` 是 `inventory` crate 导出的宏，想用路径 `inventory::submit!` 调用它，前提是**当前编译的 crate** 的 `Cargo.toml` 里声明了 `inventory` 依赖。而宏展开发生在使用派生宏的 crate（如 `ui`）里，不是在 `component` 里。若宏生成 `inventory::submit!`，那么每一个想用 `#[derive(RegisterComponent)]` 的 crate 都必须额外把 `inventory` 加进自己的依赖——包括大量根本不该关心 inventory 存在的业务 crate。事实证据：[crates/ui/Cargo.toml](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/Cargo.toml#L17) 的 `[dependencies]` 里有 `component`（第 17 行）和 `ui_macros`（第 31 行），**没有任何 `inventory`**——`ui` 之所以能编译过 `submit!`，正是因为走的是 `component::__private::inventory` 这条重导出路径。

**问题二：版本同一性。** Cargo 允许依赖图里出现同一个 crate 的多个不兼容版本。`inventory` 的集合是「按 crate 实例隔离」的：如果某个业务 crate 恰好自己依赖了另一版本的 `inventory`，它写的 `inventory::submit!` 会把节点提交进**那个版本**的集合，而 `component::init()` 遍历的是 `component` 所依赖版本的集合——节点永远到不了注册表，且不报任何错。强制走 `component::__private::inventory`（它 `pub use` 的是 `component` 锁定的那个实例）从机制上杜绝了这种静默分裂：**提交方与收集方被焊死在同一个 inventory 实例上**。

这就是标题里「摆渡」的含义：过程宏没有 `$crate`，只能生成一个以 `component` 为锚点的绝对路径，再由 `component` 出面重导出 `inventory`。`#[doc(hidden)]` 与 `__private` 这个吓人的名字共同声明「这是宏的内部通道，不是公共 API，不承诺稳定」。但要注意：**这条路径不限定宏展开使用**——手写代码同样可以走（`toggle_button.rs` 第 408 行就是手写的），它是绕开派生宏做手动注册的官方后门。

#### 4.3.2 核心流程

```text
ui_macros（过程宏 crate，编译期运行）
  └─ quote! 生成文本：component::__private::inventory::submit! { ... }
        ↓  这段文本被放回使用方的源码里继续编译
ui（使用派生宏的 crate，运行时已依赖 component）
  └─ 解析 component::__private::inventory
        = component 依赖锁定的那个 inventory 实例
        = collect!(ComponentFn) 所在的同一实例   → 集合必然对得上
```

对照平行案例可以确认这是仓库级惯用法，而非 `component` 的独家发明：

- gpui：[crates/gpui/src/gpui.rs:76-78](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui/src/gpui.rs#L76-L78) 的 `pub mod private { pub use inventory; }`，被 [crates/gpui_macros/src/register_action.rs:41](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_macros/src/register_action.rs#L41) 生成的 `gpui::private::inventory::submit!` 使用——与 `component` 的写法逐字对应。
- feature_flags：[crates/feature_flags/src/store.rs:29-32](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/feature_flags/src/store.rs#L29-L32) 的 `#[doc(hidden)] pub mod __private { pub use inventory; }`，供第 39 行声明宏 `register_feature_flag!` 的展开使用（声明宏版用 `$crate::__private::...`，比过程宏多了 `$crate` 可用）。

#### 4.3.3 源码精读

重导出本体——[crates/component/src/component.rs:41-45](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L41-L45)：

```rust
/// Private internals for macros.
#[doc(hidden)]
pub mod __private {
    pub use inventory;
}
```

`pub` 是必须的（宏展开要在别的 crate 里访问它），`#[doc(hidden)]` 让它不出现在文档里，doc 注释 "Private internals for macros" 直白说明了用途。

消费它的宏生成物——[crates/ui_macros/src/derive_register_component.rs:24-26](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/src/derive_register_component.rs#L24-L26)：

```rust
component::__private::inventory::submit! {
    component::ComponentFn::new(#register_fn_name)
}
```

为什么宏只能生成这样的路径？看 `ui_macros` 自己的依赖表——[crates/ui_macros/Cargo.toml:15-21](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui_macros/Cargo.toml#L15-L21)：

```toml
[dependencies]
quote.workspace = true
syn.workspace = true

[dev-dependencies]
component.workspace = true
ui.workspace = true
```

`component` 只出现在 `[dev-dependencies`（供宏自己的测试用），普通依赖里只有 `quote` 和 `syn`。过程宏 crate 在编译用户代码时无法 `use component::...`，它生成的 `component::` 前缀路径完全依赖**使用方**的依赖图来解析——这就是 4.3.2 流程图里「摆渡」的起点。这也再次解释了 u1-l1 早已指出的架构事实：`ui_macros` 不依赖 `component`，却与它隔着使用方紧密协作。

#### 4.3.4 代码实践：用 grep 验证惯用法

1. **实践目标**：亲手验证 4.3.1 的两个论断——`ui` 不直接依赖 `inventory`；`__private` 重导出是仓库级模式。
2. **操作步骤**（在仓库根目录执行）：

   ```bash
   # 论断一：ui 的正式依赖里没有 inventory（预期无输出）
   grep -n "^inventory" crates/ui/Cargo.toml

   # 论断二：全仓库有多少处「重导出 inventory 给宏用」
   grep -rn "pub use inventory" crates --include=*.rs

   # 论断三：都有谁在消费这类路径
   grep -rn "::inventory::submit" crates --include=*.rs
   ```

3. **需要观察的现象**：论断二的输出应包含 `component/src/component.rs`、`gpui/src/gpui.rs`、`feature_flags/src/store.rs` 等至少三四处；论断三的输出里既有宏 crate 生成的调用（如 `ui_macros`、`gpui_macros`、`settings_macros` 源文件中的 `quote!` 块），也有 `ui/src/components/button/toggle_button.rs` 这样的手写调用。
4. **预期结果**：确认「重导出点在使用方共同的祖先 crate、消费点在宏展开与手写代码两侧」的结构在多套机制中重复出现。若输出与预期不符（例如 grep 不到），说明仓库结构相对当前 HEAD 已有变化，请以 `git log -- <文件>` 追溯（本讲基于 HEAD `28c0f4aef8` 验证）。

#### 4.3.5 小练习与答案

**练习 1**：既然给 `ui` 的 `Cargo.toml` 加一行 `inventory.workspace = true` 就能直接写 `inventory::submit!`，为什么不这么做？

**参考答案**：能编译，但代价是（1）每个想手写或使用派生宏的 crate 都要各自加这个依赖，把实现细节泄漏给所有使用方；（2）打开版本分裂的口子——使用方自己引入的 `inventory` 与 `component` 锁定的可能不是同一实例，提交会静默落入另一个集合。走 `component::__private::inventory` 一劳永逸地把提交方绑到收集方的实例上。

**练习 2**：`__private` 模块加了 `#[doc(hidden)]`，为什么还保持 `pub`？

**参考答案**：宏展开发生在别的 crate，路径必须对外可见才能通过编译，所以可见性只能是 `pub`；`#[doc(hidden)]` 与下划线开头的名字负责在「语义上」劝退普通用户——表达「不承诺稳定、不属于公共 API」，而非物理禁止访问。

**练习 3**：绕开派生宏，直接手写 `component::__private::inventory::submit! { component::ComponentFn::new(my_fn) }` 提交一个自定义函数，会发生什么？

**参考答案**：合法且生效。`inventory` 的收集不区分节点来自宏展开还是手写，`init()` 会一视同仁地执行它。效果上等同于派生宏（这正是下一节综合实践要验证的命题）；但要记住 `__private` 属于内部通道，上游改版时这里的写法可能跟着变，长期维护的代码优先用派生宏，手写通道留给泛型组件这类派生宏覆盖不了的场景。

## 5. 综合实践：手写一条与派生宏等价的注册链

把本讲三个模块串起来：不看派生宏，亲手完成「定义组件 → 手写注册函数 → 经 `__private` 通道提交 → 由 `init()` 执行」的全链路，并验证它与派生宏产物等价。

1. **实践目标**：为一个练习组件手写注册，在组件预览中看到它，再换成派生宏复验，确认两种方式殊途同归。

2. **操作步骤**：

   - 编辑 `crates/ui/src/components/divider.rs`（选它是因为该文件已在 `ui` 的模块树里，且第 3 行已有 `use crate::prelude::*;`，`Component`、`single_example` 等符号都在作用域内）。在文件末尾追加：

     ```rust
     // ── 示例代码：本讲练习，验证后请删除，勿提交上游 ──
     pub struct PracticeCard;

     impl Component for PracticeCard {
         fn scope() -> ComponentScope {
             ComponentScope::None // 不指定分组，落入 Unsorted 尾部分组
         }
         fn description() -> &'static str {
             "练习组件：手写 inventory 注册链路。"
         }
         fn preview(_window: &mut Window, _cx: &mut App) -> AnyElement {
             single_example("Hello", div().child("practice card").into_any_element())
         }
     }

     fn register_practice_card() {
         component::register_component::<PracticeCard>();
     }

     component::__private::inventory::submit! {
         component::ComponentFn::new(register_practice_card)
     }
     ```

   - 编译检查：`cargo check -p ui`。
   - 运行预览：`cargo run -p component_preview --example component_preview`，在过滤框输入 `PracticeCard`，或在 `Unsorted` 分组里翻找。
   - 等价性对照：给 `PracticeCard` 改成 `#[derive(RegisterComponent)]`（该文件第 36 行的 `Divider` 已在用这个派生，无需新增导入），删掉手写的 `register_practice_card` 和 `submit!` 块，再次运行预览。

3. **需要观察的现象**：
   - 手写方式下，`PracticeCard` 出现在预览中，名称显示为 `PracticeCard`（`name()` 默认取 `std::any::type_name` 的完整路径，界面展示的是去前缀短名——u2-l1 的结论在这里落地）。
   - 两种方式下出现的位置、名称、描述、preview 内容完全一致。

4. **预期结果**：两种方式等价。原因在源码层面一目了然：派生宏生成的最后一段（`derive_register_component.rs` 第 24-26 行）与你手写的内容逐token对应——同样构造 `component::ComponentFn::new(某函数)`、同样走 `component::__private::inventory::submit!` 提交；差别只在注册函数的名字（宏生成 `__component_registry_internal_register_PracticeCard`，你手写 `register_practice_card`）和它是机器生成还是人写的。装载期两者都会作为节点挂进集合，`init()` 无差别执行。若想再进一步，可在 `component::init()` 临时加一行 `dbg!(f.0 as usize)` 观察每次执行的函数指针地址（实验后删除；具体输出待本地验证）。

## 6. 本讲小结

- 注册被拆成两阶段：**登记**（`submit!` 在编译期展开成静态节点，装载期、`main` 之前挂进集合）与**执行**（`component::init()` 遍历 `inventory::iter::<ComponentFn>()` 逐个调用函数指针，最终经 `register_component::<T>()` 写入 `COMPONENT_DATA`）。
- `ComponentFn` 是 `fn()` 的 const 构造 newtype，把「注册动作」变成可放进静态内存、可被集合枚举的值；`collect!(ComponentFn)` 在属主 crate 声明且仅此一处，`init()` 里的 `iter` 是全树唯一消费点。
- 铁律：只有被链接进二进制的 crate 才贡献节点——不依赖组件 crate 的二进制里 `init()` 遍历的是空集；`iter` 的顺序取决于链接顺序（仓库注释原话），但注册表按 id 存 `HashMap`，顺序无关。
- `component::__private::inventory` 重导出一次解决两个问题：让不必依赖 `inventory` 的使用方 crate（如 `ui`）也能 `submit!`（依赖可见性），并把提交方焊死在 `collect!` 所在的同一 inventory 实例上（版本同一性）。
- `ui_macros` 不依赖 `component`（只在 dev-dependencies 里出现），它生成的 `component::` 前缀代码全靠使用方的依赖图解析——泛型组件（如 `ToggleButtonGroup`）则完全绕开派生宏，手写注册函数并钉死具体实例。

## 7. 下一步学习建议

- 下一讲（u2-l4）将完整精读 `derive_register_component.rs` 的三段生成物：本讲只拆了其中的 `submit!` 一段，还有 `const _: () = {...}` 编译期断言技巧和注册函数命名规则值得逐行吃透，并系统对比派生与手写两条路径的取舍。
- 横向对照阅读另外几套同构机制，加深对「collect 属主 + `__private` 摆渡 + init 执行」范式的理解：`crates/gpui/src/gpui.rs` 的 `private` 模块与 `crates/gpui_macros/src/register_action.rs` 的 action 注册；`crates/feature_flags/src/store.rs` 的声明宏版本 `register_feature_flag!`。
- 若想验证装载期行为，可以在本地用一个最小 gpui 工程做 4.2.4 的思考实验（只依赖 `component` 的二进制里 `components().len()` 为 0），把「链接包含性」从推论变成亲手测得的事实。
