# u2-l2 注册表与元数据：COMPONENT_DATA、ComponentRegistry、ComponentMetadata

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `register_component` 如何把 `Component` trait 七个关联函数的「答案」逐字段固化成 `ComponentMetadata` 这个纯值类型，并解释为什么用值快照而不是 `dyn Component`。
2. 解释 `ComponentMetadata` 中 `fn` 指针字段和 `SharedString` 字段各自解决了什么问题。
3. 画出 `COMPONENT_DATA`（`LazyLock<RwLock<ComponentRegistry>>`）的**写入时机**与**读取时机**，并说明「快照语义」对调用顺序的约束。
4. 逐个掌握 `ComponentRegistry` 的七个读 API（`previews`、`sorted_previews`、`components`、`sorted_components`、`component_map`、`get`、`len`），并知道 `component_preview` 实际用了哪几个、在哪一行用。
5. 掌握 `scopeless_name()` 的去前缀技巧，以及 `get()` 的正确键值——**完整类型路径**，不是界面上的短名。

## 2. 前置知识

本讲承接 u2-l1 的核心结论：**`Component` trait 的七个方法全部是无 `self` 的关联函数，只在 `register_component` 里被求值一次**。本讲要回答的下一个问题是：求值出来的答案存到了哪里、长什么样、怎么查。为此需要先理解几个 Rust 基础概念：

- **值快照（snapshot）vs trait 对象**：`Box<dyn Component>` 这样的 trait 对象只能调用 `&self` 方法（虚表里只有实例方法）。本 trait 的方法全是关联函数（没有 `self`），根本无法通过 `dyn` 调用。所以唯一的选择是：在注册那一刻把每个方法的返回值**抄录**进一个普通结构体——这个结构体就是 `ComponentMetadata`，一块可克隆、可存储的「纯数据」。
- **fn 指针**：类型写法 `fn(&mut Window, &mut App) -> AnyElement`，指向函数代码本身的裸指针。它是 `Copy` 的、不占堆分配，比较的是地址。前提是被指向的函数没有捕获环境——关联函数恰好满足。
- **`SharedString`**：gpui 的字符串类型（定义在 `crates/gpui_shared_string/gpui_shared_string.rs`），内部是 `SmolStr`，要么包裹 `&'static str`、要么包裹 `Arc<str>`，克隆只是引用计数。`new_static` 是 `const fn`，把 `'static` 字符串零拷贝地包进去（[gpui_shared_string.rs:L27](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_shared_string/gpui_shared_string.rs#L27)）。
- **`LazyLock`**：标准库的线程安全「懒初始化」包装。全局变量第一次被访问时执行一次初始化闭包，之后所有线程共享结果——避免「多个静态量之间谁先初始化」的顺序问题。
- **`RwLock`（parking_lot 版）**：「多读者单写者」锁。`read()` 拿读锁（多个读者可并存），`write()` 拿写锁（独占）。注意它**不可重入**：同一线程持有读锁期间再去拿写锁（或反过来）会永远阻塞。
- **`HashMap` 对键的要求**：键类型必须实现 `Eq + Hash`。`ComponentId` 恰好派生了这两个 trait。
- **复杂度记号**：哈希查找平均 \( O(1) \)，遍历是 \( O(n) \)，排序是 \( O(n \log n) \)——本讲会看到三种成本分别对应哪些 API。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [crates/component/src/component.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs) | trait 定义 + 注册表 + 枚举 | **主战场**：`register_component`（L47-L61）、`COMPONENT_DATA`（L63-L64）、`ComponentRegistry`（L66-L104）、`ComponentId`（L106-L107）、`ComponentMetadata`（L109-L158） |
| [crates/component_preview/src/component_preview.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs) | 预览应用 | **唯一消费者**：`components()`/`sorted_components()`/`component_map()` 的调用现场（L124-L142）、组内 `sort_name()` 重排（L276/L300/L315）、`get` 回退（L536-L551）、fn 指针最终调用（L979） |
| [crates/component_preview/examples/component_preview.rs](https://github.com/zed-industries-zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs) | 独立宿主二进制 | 主实践的调试代码挂载点（L32 `component::init()` 之后） |
| [crates/gpui_shared_string/gpui_shared_string.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_shared_string/gpui_shared_string.rs) | `SharedString` 定义 | `new_static`、`PartialEq<&str>` 的实现（L27、L104），解释元数据字符串字段的选型 |
| [crates/ui/src/components/button/button.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/button.rs) 等 | 组件实现 | `sort_name()` 覆写实例：`Button` → `"ButtonA"`（L520-L522）、`IconButton` → `"ButtonB"`（[icon_button.rs:L278-L280](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/icon_button.rs#L278-L280)）、`SpinnerLabel` → `"Spinner Label"`（[spinner_label.rs:L189-L191](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/label/spinner_label.rs#L189-L191)） |

## 4. 核心概念与源码讲解

本讲规格要求的三个最小模块——`ComponentMetadata`、`COMPONENT_DATA` 全局态、`ComponentRegistry`——分别对应 4.1、4.2、4.3；4.4 把三者串成一条真实的消费链路，并完成本讲的主实践。

### 4.1 ComponentMetadata：把 trait 的答案固化成值

#### 4.1.1 概念说明

u2-l1 已经确认：`Component` 的七个方法都是关联函数，注册时被 `register_component` 逐个求值**一次**。求值结果需要一个归宿。

为什么不能直接把「组件类型」存起来（比如 `Box<dyn Component>`）？两个原因：

1. **语法上不可能**：trait 的方法没有 `self`，通过 `dyn` 指针无法调用关联函数——虚表里根本没有它们的位置。
2. **没有实例可存**：这些组件类型很多根本没有运行期实例，注册的是「类型的信息」，不是「对象」。

所以这套架构选择把信息**抄录成纯数据**：`ComponentMetadata` 是一个普通结构体，每个字段对应一个 trait 方法的返回值。它是「trait 答案的化石」——之后预览界面与这个化石打交道，再也不需要碰 trait 本身。

这个设计有一个直接推论：**元数据是静态的**。注册那一刻之后，组件的 name/scope/description 就固定了；你改不了它，也不应该改它。

#### 4.1.2 核心流程

`register_component::<T>()` 的执行过程：

```text
register_component::<T>():
    id = T::id()                          # 求值一次，后面既当键又当字段
    metadata = ComponentMetadata {
        id:          id.clone(),           # ComponentId（&'static str 的 newtype）
        description: SharedString::new_static(T::description()),
        name:        SharedString::new_static(T::name()),
        preview:     T::preview,           # 注意：没有括号！存的是函数指针本身
        scope:       T::scope(),
        sort_name:   SharedString::new_static(T::sort_name()),
        status:      T::status(),
    }
    COMPONENT_DATA.write().components.insert(id, metadata)
```

七个字段逐个看「类型选型」：

| 字段 | 类型 | 为什么是这个类型 |
| --- | --- | --- |
| `id` | `ComponentId`（`&'static str` 的 newtype） | 做 `HashMap` 的键需要 `Eq + Hash`；`&'static str` 免分配、免所有权 |
| `description` / `name` / `sort_name` | `SharedString`（经 `new_static` 包裹） | 源头都是 `&'static str`，零拷贝包进 `SharedString` 后，克隆只是引用计数，元数据可以随便 `clone()` |
| `preview` | 裸 fn 指针 `fn(&mut Window, &mut App) -> AnyElement` | 关联函数无捕获环境，一个指针就是它的全部；`fn` 指针是 `Copy` 的，克隆元数据 = 复制 8 字节地址 |
| `scope` / `status` | 枚举值 | 简单 `Clone`，无任何堆开销 |

注意 `preview: T::preview` 这一行**没有括号**——在 Rust 里 `T::preview` 是「取该函数的指针」，`T::preview(window, cx)` 才是「调用它」。注册时存指针，渲染时才调用，这正是 u2-l1 讲过的「注册不求值、渲染才执行」在数据结构层面的体现。

#### 4.1.3 源码精读

抄录现场——七个关联函数唯一被集中调用的地方：

[component.rs:L47-L61](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L47-L61) —— `register_component` 逐字段求值并构造 `ComponentMetadata`，最后以 `id` 为键插入全局注册表。`id.clone()` 出现两次（一次存字段、一次当键），因为 `ComponentId` 克隆只是复制一个 `&'static str` 指针，成本为零。

化石本体——七个字段与 trait 的七个方法一一对应：

[component.rs:L109-L118](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L109-L118) —— `ComponentMetadata` 只派生了 `Clone`。没有 `Debug`、没有 `PartialEq`——所以你不能 `println!("{:?}", metadata)`，只能通过访问器逐项打印（这会影响本讲实践的写法）。

访问器层，重点看 `scopeless_name`：

[component.rs:L145-L153](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L145-L153) —— `scopeless_name()` 是「去前缀技巧」的完整实现：`split("::").last()` 取路径最后一段（`ui::components::divider::Divider` → `Divider`），`unwrap_or(&self.name)` 兜底，然后**必须 `to_string()` 拥有这个切片**（它借用的是 `&self.name`，生命周期出不了函数），最后 `.into()` 转成 `SharedString`。这一行链式调用解释了为什么它返回 `SharedString` 而不是 `&'static str`——它每次调用都在生产一个新字符串。

为什么需要 `scopeless_name`？回顾 u2-l1 的派生链：`name()` 默认取 `std::any::type_name::<Self>()`（完整路径，保证全局唯一、可做键），但**界面上要显示短名**。两种诉求分别由 `name()` 和 `scopeless_name()` 承担——前者面向机器（键、过滤），后者面向人（标题、侧栏）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「元数据没有 `Debug`，只能用访问器」以及「`SharedString` 与 `&'static str` 的零拷贝关系」。

1. 打开 [component.rs:L109-L118](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L109-L118)，数一数派生属性：只有 `#[derive(Clone)]`。
2. 在仓库根目录执行 `git grep -n "derive(Debug, Clone)" -- crates/component/src/component.rs`，对比 `ComponentId`（L106，有 `Debug`）与 `ComponentMetadata`（无）。
3. 打开 [gpui_shared_string.rs:L27](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_shared_string/gpui_shared_string.rs#L27)，确认 `new_static` 是 `pub const fn`——意味着包裹 `'static` 字符串发生在编译期，零运行期分配。

**需要观察的现象**：`ComponentMetadata` 的派生列表与 `ComponentId` 的派生列表不同。

**预期结果**：`ComponentId` 派生了 `Debug, Clone, PartialEq, Eq, Hash`（可打印、可做键）；`ComponentMetadata` 只派生 `Clone`。因此本讲主实践中打印元数据只能写 `metadata.id().0`、`metadata.name()` 这样的访问器形式。（grep 结果待本地验证，随代码演进变化。）

#### 4.1.5 小练习与答案

**练习 1**：如果想让元数据多携带一项「作者」信息（`author: SharedString`），从 trait 到界面要改哪几处？

**答案**：至少五处——① `Component` trait 加一个返回 `&'static str` 的方法（通常带默认实现 `""`）；② `ComponentMetadata` 加 `author` 字段；③ `register_component` 加一行 `author: SharedString::new_static(T::author())`；④ `ComponentMetadata` 加访问器 `pub fn author(&self)`；⑤ 消费端（`component_preview` 的头部渲染）加一行 `Label`。这正是 u2-l1 练习 2「加 trait 方法的真实成本」在数据结构侧的展开：快照架构下，**每个新增信息都要走完这条五段路**，没有捷径。

**练习 2**：`preview` 字段为什么能用裸 `fn` 指针，而很多 Rust 框架里回调要用 `Box<dyn Fn>`？

**答案**：`Box<dyn Fn>` 存在的意义是让闭包**携带捕获的环境变量**；而 `Component::preview` 是关联函数，没有 `self`、也没有任何捕获——它就是一段固定的代码。裸 `fn` 指针足以表达「一段固定代码」，且它是 `Copy` 的、不占堆分配，让 `ComponentMetadata` 的克隆永远是浅拷贝。反过来，`preview` 能写成关联函数也正是这套设计的前提：**预览函数不依赖组件实例的状态**（有状态预览在函数体内部自己构造状态，见 u4-l1 的 `reset_key`）。

**练习 3**：`scopeless_name()` 为什么不能返回 `&'static str`？

**答案**：`split("::").last()` 返回的 `&str` 是 `self.name` 的切片，生命周期挂在 `&self` 借用上，出了函数体就失效。要返回独立值必须先 `to_string()` 拥有它——所以返回类型只能是拥有型字符串（`SharedString`）。每次调用都新分配一个小字符串，是这个便捷访问器的代价（预览界面调用频率很低，不值得缓存）。

### 4.2 COMPONENT_DATA：全局注册表的读写时机

#### 4.2.1 概念说明

元数据造出来之后要放进一个所有 crate 都能访问的地方。注册方（`ui`、`settings_ui`……）和读取方（`component_preview`）互相不依赖（见 u1-l1 的依赖图），它们唯一的汇合点就是 component crate 里的一个**进程级全局变量**：

```rust
pub static COMPONENT_DATA: LazyLock<RwLock<ComponentRegistry>> =
    LazyLock::new(|| RwLock::new(ComponentRegistry::default()));
```

三层包装各司其职：

- **`static`**：全局唯一实例，注册方与读取方的交汇点。
- **`LazyLock`**：第一次被访问时才构造，线程安全。这里用它还有一个朴素的原因：`ComponentRegistry::default()` 不是 `const fn`，没法直接写成 `const` 初始化的静态量；懒初始化是标准解法，顺带消除了静态初始化顺序问题。
- **`RwLock`（parking_lot）**：注册期「多写」、预览期「多读」的读写分离。虽然 Zed 的 UI 逻辑基本都在主线程，但类型系统并不假设这一点——锁把「谁在读、谁在写」的契约写进了类型。

#### 4.2.2 核心流程

完整生命周期时间线（承接 u1-l2 讲过的两阶段注册）：

```text
进程启动
  │ 【链接期】inventory 收集 ComponentFn —— 只是登记函数指针，未执行
  ▼
component::init()                          ← workspace::init 首行 / example main 首行
  │  for f in inventory::iter::<ComponentFn>():
  │      (f.0)()                           ← 执行每个注册函数
  │          └─ register_component::<T>()
  │               ├─ 求值 T 的七个关联函数，构造 ComponentMetadata
  │               ├─ COMPONENT_DATA.write()   ← 拿【写锁】（每个组件各拿一次）
  │               └─ components.insert(id, metadata)
  ▼
component::components()                    ← 预览应用构造时调用（component_preview L124）
  │  COMPONENT_DATA.read()                 ← 拿【读锁】，读完全表立即放锁
  │  .clone()                              ← 整表克隆，得到一份「快照」
  ▼
ComponentPreview::new —— 之后 UI 只看这份快照，不再回读全局表
```

由此得到**读写时机表**：

| 操作 | 触发者 | 时机 | 频率 |
| --- | --- | --- | --- |
| 写（`write()` + `insert`） | `register_component` | 仅 `component::init()` 执行期间 | 每组件一次 |
| 读（`read()` + `clone`） | `components()` | 预览界面构造时 | 实际一次（component_preview L124） |

两个关键性质（都在 u1-l2 埋过伏笔，这里给出机制层面的解释）：

- **写入幂等**：`insert` 以 `id` 为键，同一组件注册两次只是「覆盖成相同的值」。所以 `component::init()` 调两次无害。
- **快照语义**：`components()` 返回的是整表**克隆**。拿到手之后，哪怕有人再往全局表里注册新组件，这份快照也不会变。这就是「`init()` 必须早于第一个消费者」的根本原因——不是注册表会变，而是**消费者手里那份不会变**。

还有一个值得注意的事实：`COMPONENT_DATA` 虽然声明为 `pub`，但整个仓库里直接碰它的代码只有 component crate 自己——读（L22）、写（L59）、定义（L63）。外界一律走 `components()` / `register_component()` 这两个门面函数。`pub` 在这里提供的是「外部测试/工具可以访问」的可能性，树内则保持了封装纪律。

#### 4.2.3 源码精读

读路径——全仓库唯一的注册表读取入口：

[component.rs:L21-L23](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L21-L23) —— `components()` 拿读锁、克隆整表、返回拥有型 `ComponentRegistry`。读锁在 `clone()` 完成的瞬间释放，调用方拿到的快照与锁再无关系。

写路径：

[component.rs:L59-L60](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L59-L60) —— `register_component` 末尾两行：拿写锁（`let mut data = ...`），`insert(id, metadata)`。写锁的生命周期就是这两行——函数结束即释放，绝不会横跨其他调用。

全局态定义：

[component.rs:L63-L64](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L63-L64) —— `COMPONENT_DATA` 的双层包装：`LazyLock` 管初始化，`RwLock` 管并发。`ComponentRegistry::default()` 起点是空表。

#### 4.2.4 代码实践

**实践目标**：验证「`COMPONENT_DATA` 的树内触点只有三处、且全在 component.rs 内部」这一封装论断。

1. 在仓库根目录执行 `git grep -n "COMPONENT_DATA" -- crates/`。
2. 把命中行按文件归类，与 4.2.3 的三个行号（L22、L59、L63）对照。
3. 再执行 `git grep -n "component::components()" -- crates/`，确认读门面的调用方只有 `component_preview`（本讲义目录下的命中忽略即可）。

**需要观察的现象**：两个 grep 的命中范围。

**预期结果**：`COMPONENT_DATA` 只在 `crates/component/src/component.rs` 内出现三处；`component::components()` 只在 `crates/component_preview/src/component_preview.rs` L124 出现。若命中了其他 crate，说明代码相对本讲撰写时的 HEAD（28c0f4ae）已有演进。（待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：如果在 `register_component` 内部（拿着写锁时）误调用了一句 `components()`，会发生什么？

**答案**：死锁。`components()` 要拿读锁，而当前线程已持有写锁；parking_lot 的 `RwLock` 不可重入，也不支持锁升级，该线程会永远阻塞在 `read()` 上。这是使用全局 `RwLock` 最典型的陷阱——**门面函数之间不能互相嵌套调用**。当前代码没有这个问题，因为 `register_component` 与 `components()` 的调用链完全分离。

**练习 2**：把 `RwLock` 换成 `Mutex` 功能上也成立吗？为什么选 `RwLock`？

**答案**：功能上成立——本场景的读和写从不在同一线程嵌套发生，`Mutex` 也能正确互斥。选 `RwLock` 是因为访问模式是典型的「启动期集中写、之后零星读」，读写锁允许读操作并存（多个线程同时 `components()` 不会互相排队），语义上也更准确地表达了「这里读多写少」。

**练习 3**：`ComponentPreview` 构造完成之后，某个新 crate 又注册了一个组件（假设运行期动态加载），预览界面会显示它吗？

**答案**：不会。`ComponentPreview::new` 在 L124 拿走快照后，全局表的变化与它无关；界面的组件列表、`component_map` 全部来自那份快照。当然在 Zed 的实际形态里组件集在编译期就固定了，这只在理论上的动态加载场景才会成为问题——但「快照不更新」这个语义边界，写消费代码的人必须心里有数。

### 4.3 ComponentRegistry：七个读 API 全景

#### 4.3.1 概念说明

`ComponentRegistry` 本体薄得惊人——就是一个 `HashMap` 外面套了层壳：

```rust
#[derive(Default, Clone)]
pub struct ComponentRegistry {
    components: HashMap<ComponentId, ComponentMetadata>,
}
```

它的价值全在 `impl` 块提供的**七种读视图**上。理解这七个 API 的关键不是背签名，而是看清它们返回的三种形态各自为谁服务：

1. **借用视图**（`previews`、`components`、`get`）：返回 `&ComponentMetadata`，零克隆，但生命周期被注册表借用绑住——只能在持有注册表的地方临时用。
2. **拥有视图**（`sorted_components`、`sorted_previews`）：克隆成 `Vec<ComponentMetadata>`，可以存进结构体字段、跨函数传递——**调用方要长期持有时的选择**。
3. **整表视图**（`component_map`）：把整个 `HashMap` 克隆走，调用方获得与注册表同构的 O(1) 查询能力。

#### 4.3.2 核心流程

先给全景表（「树内调用方」一列是本讲逐一核实过的，这一点很重要——**公共 API 不等于有人用**）：

| 方法 | 返回类型 | 顺序 | 树内调用方 |
| --- | --- | --- | --- |
| `previews()` | `impl Iterator<Item = &ComponentMetadata>` | 无序 | 仅 `sorted_previews`（L77）内部使用 |
| `sorted_previews()` | `Vec<ComponentMetadata>` | 按 `name()` | 无外部调用方 |
| `components()` | `Vec<&ComponentMetadata>` | 无序 | 无外部调用方 |
| `sorted_components()` | `Vec<ComponentMetadata>` | 按 `name()` | `component_preview` L125 |
| `component_map()` | `HashMap<ComponentId, ComponentMetadata>` | — | `component_preview` L141 |
| `get(&ComponentId)` | `Option<&ComponentMetadata>` | — | 无直接调用方（见下） |
| `len()` | `usize` | — | 无调用方 |

三层信息值得强调：

**① 排序发生在两层、用两个不同的键。** 注册表层 `sorted_components()` 按 **`name()`（完整类型路径）** 排序（L89）；而 `component_preview` 的 `scope_ordered_entries()` 在**每个作用域分组内部**又按 **`sort_name()`** 重新排序（[component_preview.rs:L275-L277](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L275-L277)、L300、L315）。所以：

- 「Button 家族聚在一起」的界面效果来自**第二层**的 `sort_name()`——`Button` 覆写为 `"ButtonA"`（[button.rs:L520-L522](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/button.rs#L520-L522)）、`IconButton` 覆写为 `"ButtonB"`（[icon_button.rs:L278-L280](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/button/icon_button.rs#L278-L280)）。
- 第一层按完整路径排序的结果主要服务于**索引域**：`ComponentPreview::new` 用 `sorted_components.len()` 初始化列表状态（L131），`selected_index` 在这个向量上定位。

**② `get` 的 \( O(1) \) 与遍历的 \( O(n) \)。** `get` 是哈希查找，平均 \( O(1) \)；`sorted_components` 要克隆加排序，\( O(n \log n) \)。已知 `id`（比如从持久化数据恢复上次看的页面）时用 `get`；只有模糊条件（「找界面短名叫 Divider 的组件」）才被迫遍历。

**③ `registry.get` 与消费者的 `map.get` 是门面关系。** `component_preview` 在 L536 查询用的是**自己字段的** `self.component_map.get(component_id)`（构造时经 `component_map()` 克隆来的），不是 `ComponentRegistry::get`。两者语义等价（后者就是转调前者，见 L97-L99），区别只在于「拿着注册表查」还是「拿着构造时克隆的 map 查」。树内之所以没有 `ComponentRegistry::get` 的直接调用方，正是因为消费者选择了「克隆一份、此后自给自足」的模式。

#### 4.3.3 源码精读

结构体与两个「无外部消费者」的视图：

[component.rs:L66-L80](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L66-L80) —— `ComponentRegistry` 就是 `HashMap` 的 newtype 壳（派生 `Default` + `Clone`，后者支撑 4.2 的整表快照）。`previews()` 返回借用迭代器，`sorted_previews()` 在它基础上克隆、按 `name()` 排序。

三个「有人用」的视图加两个工具方法：

[component.rs:L82-L103](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L82-L103) —— `components()`（借用 `Vec`）、`sorted_components()`（克隆后按 `a.name()` 排序——注意排序键是 `name()` 而非 `sort_name()`，这是 4.3.2 ① 的出处）、`component_map()`（整表克隆）、`get()`（转调 `HashMap::get`）、`len()`。

`get` 的返回是 `Option<&ComponentMetadata>`——「查不到」是一等公民。它的防弹价值在消费端体现：

[component_preview.rs:L536-L551](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L536-L551) —— 渲染单个组件页时用 `self.component_map.get(component_id)` 查找；查不到（例如代码重构后组件 `id` 变了，而持久化数据里存的还是旧 `id`）就渲染 `"Component not found"` 提示而不是崩溃。这就是 `get` 返回 `Option` 的意义：**注册表的查询必须容错**。

组内按 `sort_name()` 重排的现场：

[component_preview.rs:L275-L277](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L275-L277) —— `scope_ordered_entries` 对每个作用域分组执行 `components.sort_by_key(|(c, _)| c.sort_name())`。顺带一个诚实的观察：L300、L315 在把分组取出后又各自重排了一遍——对同一份数据排了两次序，结果不变，属于冗余但不影响正确性的代码（阅读时不要被它迷惑，以为第一处排序没用）。

#### 4.3.4 代码实践

**实践目标**：亲手验证 4.3.2 全景表的「树内调用方」一列，体会「公共 API ≠ 有人使用」。

1. 在仓库根目录依次执行：
   - `git grep -n "\.sorted_components()" -- crates/`
   - `git grep -n "\.component_map()" -- crates/`
   - `git grep -n "\.sorted_previews()\|\.previews()" -- crates/`
   - `git grep -n "registry\.get(\|\.len()" -- crates/component_preview/src/`
2. 把每条命令的命中行与全景表对照。

**需要观察的现象**：哪些 API 命中 `component_preview`，哪些只命中 `component.rs` 自身。

**预期结果**：`sorted_components` 与 `component_map` 各命中 `component_preview` 一处（L125、L141）；`previews`/`sorted_previews` 只在 `component.rs` 内部出现；`component_preview` 里的 `.len()` 是 `Vec::len` / `String::len` 而非 `ComponentRegistry::len`。（待本地验证，随代码演进变化。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `components()` 返回 `Vec<&ComponentMetadata>`，而 `sorted_components()` 返回 `Vec<ComponentMetadata>`（拥有型）？

**答案**：用途不同。`components()` 是内部工具（给 `sorted_components` 收集数据用），借用即可；`sorted_components()` 的调用方 `ComponentPreview` 要把结果**存进自己的结构体字段**（`components: sorted_components`，L142）——结构体字段不能持有借来的引用（除非给整个结构体加生命周期参数，会传染到所有使用处）。拥有型 `Vec` 配合 `ComponentMetadata` 的廉价 `Clone`（`SharedString` 引用计数 + `fn` 指针 `Copy`），克隆成本完全可接受。

**练习 2**：已知 `id` 要查组件、和「按界面短名模糊找组件」，分别该用哪个 API？为什么？

**答案**：前者用 `get(&id)`，平均 \( O(1) \)，例如从持久化的 `active_page_id` 恢复页面（消费端 L536 的等价操作）；后者只能遍历——`sorted_components()` 逐个比较 `scopeless_name()`，\( O(n) \)。本讲主实践会让你把两种方式都写一遍，体感差距就在「你要先知道完整 `id`」这一步。

**练习 3**：`len()` 在树内没有调用方，它是不是应该删掉？

**答案**：从「树内无用」的角度看可以删，但它是注册表最基本的自省接口——本讲主实践就会用它打印组件总数；对库crate而言，`len` 属于「成本一行、语义完整」的惯例 API（与 `is_empty` 配套的惯例在此仅实现了一半，严格说缺 `is_empty` 是不符合 Rust API 准则的，但这是小瑕疵）。保留公共自省 API 让下游消费者（比如你自己的项目）不必为了数一下组件数去克隆整表。

### 4.4 消费现场：component_preview 如何使用这套 API

#### 4.4.1 概念说明

前面三节分别解剖了三个最小模块，这一节把它们串成一条真实的链路：**一次构造、三份视图、O(1) 页面定位、fn 指针最终调用**。`ComponentPreview::new` 是全仓库唯一下载注册表快照的地方，读懂它的前十行，本讲的三个模块就全部落地了。

#### 4.4.2 核心流程

```text
ComponentPreview::new（构造一次）
  ├─ components()                    ← 读全局表，拿快照（读锁瞬间释放）
  ├─ sorted_components()             ← 视图①：按 name 排序的 Vec
  │     └─ ListState::new(len, ...) ← 用它定列表长度（索引域）
  ├─ component_map()                 ← 视图②：整表 HashMap（此后查询自给自足）
  └─ 存进字段 components / component_map

运行期
  ├─ 点击侧栏条目 → set_active_page(Component(id))
  ├─ 渲染组件页 → self.component_map.get(&id)      ← O(1) 定位
  │      └─ 查不到 → "Component not found" 回退
  └─ 渲染预览 → (metadata.preview())(window, cx)   ← fn 指针在注册后第一次被调用
```

#### 4.4.3 源码精读

快照与三份视图的下载现场：

[component_preview.rs:L124-L142](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L124-L142) —— 构造函数开头：`components()` 拿快照并 `Arc::new` 包一层（L124），`sorted_components()` 生成排序视图（L125），`sorted_components.len()` 初始化列表状态（L131），`component_map()` 克隆整表（L141），两者都存入结构体字段。此后 `ComponentPreview` 与全局注册表**再无往来**。

`scopeless_name` 的三个展示位（面向人的短名）：

- [component_preview.rs:L232](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L232) —— 过滤高亮计算用短名匹配（`scope_ordered_entries` 内部）。
- [component_preview.rs:L370](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L370) —— 侧栏列表项的文字。
- [component_preview.rs:L962](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L962) —— 组件详情页的大标题。

（一个留给 u4-l2 的细节：`filtered_components()` 在 L201 用的是**完整 `name()`** 过滤，而 `scope_ordered_entries()` 的高亮分支在 L232 用的是**短名**——两套过滤路径并存，输入 `ui::` 这样的前缀时行为会有差异。本讲只建立「键用全名、显示用短名」的分工认知，深入留给第 4 单元。）

fn 指针注册之后的最终归宿——被调用的那一刻：

[component_preview.rs:L979](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/src/component_preview.rs#L979) —— `(self.component.preview())(window, cx)`：访问器取出 fn 指针，直接调用，返回的 `AnyElement` 作为子元素挂进页面。从 `register_component` 里那行不带括号的 `preview: T::preview`（存指针），到这里带括号的调用（执行），一个组件的预览函数完成了它的旅程——**注册时复制地址，渲染时执行代码**。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手查询注册表，体会「`get` 的键是完整类型路径、不是界面短名」，并对比 `get` 与遍历两种查找方式的差异。

**一个重要更正**：本讲规格中设想的 `get(&ComponentId("ui::Divider"))` **不会命中**。`Divider` 没有覆写 `name()`（u2-l1 已确认它只实现了 `scope` 和 `description` 等，`name()` 走默认值 `std::any::type_name::<Self>()`），而它定义在 `crates/ui/src/components.rs` 的 `divider` 子模块（[components.rs:L13](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components.rs#L13)），所以真实 `id` 是完整路径 `ui::components::divider::Divider`。「先试短键失败、再从遍历中发现真键」恰恰是本实践最有教学价值的一步。

**操作步骤**：

1. 打开 [crates/component_preview/examples/component_preview.rs](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs)，在 [L32](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component_preview/examples/component_preview.rs#L32) 的 `component::init();` 之后插入以下**示例代码**（该 example 已依赖 `component`——L32 的 `component::init()` 就是证据，注册链路天然打通。规格中「直接在 component crate 内测试可能拿到空表」的顾虑是真实的：component crate 不依赖 ui，其测试二进制里没有链接任何 ui 组件的 `submit!` 产物，`init()` 后表基本是空的，所以实践必须挂在 component_preview 这个链接了 ui 的宿主里）：

   ```rust
   // —— 示例代码：临时调试片段，验证后删除 ——
   let registry = component::components();
   println!("[debug] 注册表组件总数: {}", registry.len());

   // 方式一：用「看起来合理」的短键直接 get（预期 miss）
   let by_short_name = registry.get(&component::ComponentId("ui::Divider"));
   println!("[debug] get(\"ui::Divider\") 命中: {}", by_short_name.is_some());

   // 方式二：遍历排序快照，按界面短名 scopeless_name 匹配
   for meta in registry.sorted_components() {
       if meta.scopeless_name() == "Divider" {
           println!("[debug] 遍历命中: id = {:?}", meta.id());
           println!("[debug] 真实 name = {}", meta.name());
           let by_full_path = registry.get(&meta.id());
           println!("[debug] get(完整路径) 命中: {}", by_full_path.is_some());
           println!("[debug] scope = {}, status = {}", meta.scope(), meta.status());
       }
   }
   // —— 调试片段结束 ——
   ```

   片段中所有写法都有出处：`ComponentId` 派生了 `Debug`（component.rs L106）所以能 `{:?}` 打印；`ComponentMetadata` 没有 `Debug`，所以只能用访问器（`meta.name()`、`meta.scope()`）；`meta.scopeless_name() == "Divider"` 能直接比较，是因为 `SharedString` 实现了 `PartialEq<&str>`（[gpui_shared_string.rs:L104](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/gpui_shared_string/gpui_shared_string.rs#L104)）。

2. 在仓库根目录运行：

   ```bash
   cargo run -p component_preview --example component_preview
   ```

3. 观察终端输出（窗口可以不管），记录四行 `[debug]` 日志。

**需要观察的现象**：

- 方式一输出 `命中: false`；
- 方式二输出 `id = ComponentId("ui::components::divider::Divider")`（完整路径）；
- 用完整路径再 `get` 输出 `命中: true`；
- `scope` 与 `status` 是 `Divider` 覆写后的值（`scope` 应为 Layout 相关的作用域——`Divider` 覆写了 `scope`，见 [divider.rs:L162-L163](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/ui/src/components/divider.rs#L162-L163)；`status` 未覆写则为默认 `Live`）。

**预期结果**：两种查找方式的差异一句话总结——**`get` 要的是注册键（完整类型路径，机器视角），遍历匹配的是显示短名（人视角）**；短名查询必须先经一次 \( O(n) \) 遍历「发现」真键，才能享受 \( O(1) \) 哈希查找。具体的组件总数数值、`scope` 的确切取值**待本地验证**（总数取决于链接进二进制的组件数量，随代码演进变化）。

4. 验证完毕后删除调试片段（不要把临时代码留在工作区）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ComponentPreview` 构造之后不再调用 `component::components()` 重新拿快照？

**答案**：两个原因。**正确性**上，组件集在编译期由 inventory 决定、运行期不变（`init()` 幂等，之后全局表不再被写），重读只会拿到相同内容；**设计**上，构造时一次性下载三份视图存进字段，运行期查询（每次点击、每次渲染）完全走本地字段，不用碰全局锁——把「读全局态」集中在一处，是控制全局状态影响面的常规手法。

**练习 2**：某次重构把 `Divider` 移到了别的模块（`id` 因此改变），用户上次会话持久化的 `active_page_id` 还是旧路径，打开工作区时会发生什么？

**答案**：`ComponentPreview` 反序列化恢复出旧的 `component_id`，渲染组件页时 `self.component_map.get(&id)` 查不到，走 `None` 分支渲染 `"Component not found"`（L544-L550）。应用不崩溃、用户能看到明确的提示——这就是 4.3.3 强调的「注册表查询必须容错」在真实场景中的兑现。

**练习 3**：`Arc::new(components())`（L124）这个 `Arc` 包的意义是什么？

**答案**：`components()` 返回拥有型 `ComponentRegistry`（整表克隆），`Arc` 让这份快照可以被多处共享而不需要再克隆。不过就当前代码而言，`sorted_components()` 和 `component_map()` 都是这个 `Arc` 上的方法调用，`Arc` 更多是「以备将来把快照句柄传给子视图」的低成本预留——读代码时要能分辨「必要结构」与「预留结构」，避免过度解读。

## 5. 综合实践

**任务：注册表侦探——用一份调试输出验证本讲的全部三个模块。**

在 4.4.4 主实践同一位置（example main 的 `component::init();` 之后），把调试片段升级为：

```rust
// —— 示例代码：注册表侦探（临时调试，验证后删除）——
use std::collections::BTreeMap;

let registry = component::components();
println!("[侦探] 组件总数: {}", registry.len());

// 1) 每个作用域有多少组件？（验证 scope 字段 + ComponentScope 的 Display）
let mut scope_counts: BTreeMap<String, usize> = BTreeMap::new();
for meta in registry.sorted_components() {
    *scope_counts.entry(meta.scope().to_string()).or_default() += 1;
}
println!("[侦探] 各作用域组件数: {scope_counts:?}");

// 2) 哪些组件覆写了 sort_name？（验证「两层排序」确实会产生不同顺序）
let mut renamed: Vec<(String, String)> = Vec::new();
for meta in registry.sorted_components() {
    if meta.sort_name().to_string() != meta.name() {
        renamed.push((meta.scopeless_name().to_string(), meta.sort_name().to_string()));
    }
}
println!("[侦探] sort_name ≠ name 的组件: {renamed:?}");
// —— 侦探片段结束 ——
```

运行 `cargo run -p component_preview --example component_preview`，然后完成三件事：

1. **对照第 2 节的读写时机表**：解释为什么这段代码必须插在 `component::init();` **之后**才能看到非零总数（插在之前会打印 `0`——inventory 的登记只是链接期元数据，不执行就不会写入 `COMPONENT_DATA`）。
2. **读 `renamed` 列表**：应能看到 `Button` → `ButtonA`、`IconButton` → `ButtonB` 等条目（4.3.2 已核实这五个覆写点：button.rs、icon_button.rs、button_like.rs、toggle_button.rs、spinner_label.rs）。打开组件预览界面，到对应作用域分组里找到它们，亲眼确认「组内顺序由 `sort_name` 决定」——`Button` 与 `IconButton` 在侧栏里排在同一批 `Button*` 前缀的位置。
3. **回答收尾问题**（各一句话，写进你的笔记）：
   - `ComponentMetadata` 为什么必须是一个可 `Clone` 的纯值？
   - `COMPONENT_DATA` 的一次读锁覆盖了哪些代码行？
   - `sorted_components()` 与侧栏组内排序分别用哪个字段当键？

**预期结果**：终端输出一份作用域统计和一个非空的 `renamed` 列表；界面侧栏中 Button 家族按 `ButtonA`/`ButtonB` 前缀聚组。具体数字**待本地验证**。

## 6. 本讲小结

- `register_component` 把 trait 七个关联函数的返回值一次性抄录进 `ComponentMetadata`——一个只派生 `Clone` 的纯值结构体：字符串用 `SharedString::new_static` 零拷贝包裹，`preview` 存**裸 fn 指针**（注册时复制地址、渲染时才执行），使元数据克隆永远是浅拷贝。
- `COMPONENT_DATA` 是 `LazyLock<RwLock<ComponentRegistry>>` 全局态：写锁只在 `component::init()` 期间由 `register_component` 逐组件获取；`components()` 拿读锁做**整表克隆**后立即放锁——此后消费者与全局表再无往来（快照语义，`init()` 必须早于第一个消费者的机制根源）。
- `ComponentRegistry` 七个读 API 呈三种形态（借用视图、拥有视图、整表视图），但树内真正被 `component_preview` 使用的只有 `sorted_components()`（L125）与 `component_map()`（L141）——「公共 API ≠ 有人使用」，读源码时要分清。
- 排序有两层、两个键：注册表层 `sorted_components()` 按 `name()`（完整路径，服务索引域），预览层 `scope_ordered_entries()` 在组内按 `sort_name()` 重排（服务可见顺序，Button→`ButtonA` 家族聚组的出处）。
- `get` 的键是**完整类型路径**（如 `ui::components::divider::Divider`），界面短名来自 `scopeless_name()` 的 `split("::").last()` 去前缀技巧；查不到时消费端以 `"Component not found"` 容错回退，注册表查询从不 panic。

## 7. 下一步学习建议

本讲补全了「注册之后数据长什么样、存哪里、怎么查」；下一讲 **u2-l3《inventory 分布式注册：ComponentFn 与 init()》** 回到链路最前端，解剖 4.2 时间线里被我们一笔带过的第一步：`inventory::collect!`/`submit!` 如何在**链接期**把散落在 11 个 crate 里的注册函数收进分布式切片、`inventory::iter` 遍历的又究竟是什么，以及 `__private` 模块重导出 `inventory` 解决的宏卫生问题。建议预习时先读 [component.rs:L25-L45](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/component/src/component.rs#L25-L45)（`init`、`ComponentFn`、`inventory::collect!`、`__private`），带着「为什么注册函数的类型是 `fn()` 而不是 `fn() -> ComponentMetadata`」这个问题进入下一讲。若想巩固本讲，可把第 5 节侦探实践里 `renamed` 列表中的每个组件，对照其源文件里的 `impl Component` 块读一遍——那就是元数据七个字段的出生地。
