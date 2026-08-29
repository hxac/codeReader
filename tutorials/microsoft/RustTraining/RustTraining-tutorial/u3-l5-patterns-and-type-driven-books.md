# 高级与专家书：Patterns 与 Type-Driven Correctness

## 1. 本讲目标

本讲把视线从三本桥梁书和异步深潜书，转向书架上难度最高的两本：

- **Rust Patterns**（rust-patterns-book，🟡 Advanced 级）
- **Type-Driven Correctness**（type-driven-correctness-book，🟣 Expert 级）

这两本书回答同一个问题的不同侧面：**能不能让编译器替我们证明程序是对的，而不是靠测试和运行期检查事后发现错误？**

学完本讲，你应该能够：

1. 说出 rust-patterns-book 的主题地图（泛型 → trait → newtype/type-state → PhantomData → 并发 → 智能指针 → unsafe → 宏 → 架构）。
2. 解释 type-driven-correctness-book 的核心思想「让非法状态无法表示」（make illegal states unrepresentable）。
3. 掌握四个类型层模式的原理与写法：**newtype、type-state（类型状态）、幻影类型（PhantomData）、能力令牌（capability token）**，以及它们在协议状态机（protocol state machine）中的组合运用。
4. 独立写出一个 `Door<Locked>` / `Door<Unlocked>` 最小示例，让「未解锁就开门」成为**编译错误**，并用 `cargo check` 验证错误信息。

## 2. 前置知识

本讲假设你已完成一座「桥」（推荐 c-cpp-book 或 csharp-book 的第 1–7 章），并了解 u3-l2 讲过的章节写作范式。以下概念用通俗语言补齐：

- **编译期检查 vs 运行期检查**：运行期检查是程序跑起来时用 `if` 判断「现在能不能做这件事」，忘了写就出 bug，而且只有测试覆盖到的路径才会暴露；编译期检查是把约束写进类型签名，错误调用根本无法通过编译——**所有调用点**一次性被检查，包括没人想到过的那些。
- **泛型参数**：`struct Foo<T>` 中的 `T` 是类型参数，像值的占位符一样占着一个「类型的坑」。本讲的关键技巧是：让 `T` 携带的不只是「存什么数据」，还有「现在处于什么状态」「有什么权限」这类**逻辑信息**。
- **零大小类型（Zero-Sized Type，ZST）**：没有任何字段（或字段全是 ZST）的类型，运行期不占任何一个字节。本讲所有的状态标记、权限令牌都是 ZST——所以这套「证明体系」是**零运行期开销**的。
- **`PhantomData<T>`**：标准库提供的零大小标记类型，用来告诉编译器「我这个结构体在逻辑上和 `T` 相关，虽然我并没有真的存一个 `T`」。它是把类型参数「挂」到结构体上的正规通道（Rust 会拒绝完全不使用泛型参数的结构体定义）。
- **marker trait（标记 trait）**：没有任何方法的 trait，唯一的用途是「给类型盖一个章」，让 `impl<T: SomeMarker>` 这样的约束可以按「有没有这个章」来筛选类型。
- **mdBook playground**：承接 u3-l2——书里的普通 ` ```rust ` 代码块带运行/编辑按钮，可以直接在浏览器里改和跑；` ```rust,ignore ` 只着色不可运行。本讲的实践会同时用到这两条路径。

一个贯穿全讲的类比：**把类型系统当作「证明助手」**。函数签名是定理的前提条件（precondition），返回类型是它能给出的保证（postcondition）；调用者要做的就是把「证明」——一个具有正确类型的值——递进来。

## 3. 本讲源码地图

两本书都是标准的 mdBook 目录（book.toml + src/SUMMARY.md + 章节文件），结构上与 u1-l4 解剖过的 async-book 完全一致。本讲涉及的文件：

| 文件 | 作用 |
|------|------|
| [rust-patterns-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md) | Advanced 书的主题地图：Part I 类型层模式（1–4 章），Part II 并发与运行期（5–9 章），Part III 系统与生产（10–16 章），附录含参考卡与 capstone |
| [rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md) | 本讲主力章节：newtype、type-state、类型状态 builder、Config trait、双轴 typestate，末尾附红绿灯练习 |
| [rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md) | PhantomData 的三大职责、生命周期烙印（branding）、型变 |
| [type-driven-correctness-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md) | Expert 书的主题地图：Part I 哲学，Part II 核心模式（2–11 章），Part III 集成实战（含两个 Redfish 走读），Part IV 参考 |
| [type-driven-correctness-book/src/ch01-the-philosophy-why-types-beat-tests.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch01-the-philosophy-why-types-beat-tests.md) | 「为什么类型胜过测试」：三层正确性（值 / 状态 / 协议），是整本书的总纲 |
| [type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md) | 能力令牌：ZST 作为权限证明、层级能力、生命周期受限令牌 |
| [type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md) | 协议状态机：IPMI 会话、PCIe 链路训练、固件升级三拍递进，type-state 与能力令牌的组合 |
| [type-driven-correctness-book/src/ch09-phantom-types-for-resource-tracking.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch09-phantom-types-for-resource-tracking.md) | 幻影类型追踪资源：寄存器宽度、DMA 方向、文件描述符状态 |

两本书在 README 与 xtask 的 BOOKS 常量中的级别归类可互相印证：README 中 Rust Patterns 标注「🟡 Advanced」、Type-Driven Correctness 标注「🟣 Expert」（[README.md:L53-L54](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/README.md#L53-L54)），xtask 里对应的 category 字段分别是 `"advanced"` 与 `"expert"`（[xtask/src/main.rs:L34-L45](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L34-L45)）。

主题地图速览：

- **rust-patterns-book** 按 [SUMMARY.md:L7-L12](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L7-L12) 的 Part I 先打类型层地基（Generics 全景 → Traits 深入 → Newtype 与 Type-State → PhantomData），Part II（[L16-L22](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L16-L22)）转向并发、闭包、智能指针与内部可变性，Part III（[L26-L35](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L26-L35)）覆盖错误处理、零拷贝、unsafe、宏、测试与 crate 架构，最后以「类型安全任务调度器」capstone 收束（[L39-L42](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L39-L42)）。本讲精读其 Part I。
- **type-driven-correctness-book** 的 Part II 是模式主体（[SUMMARY.md:L11-L22](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L11-L22)）：类型化命令接口、单次使用类型、能力令牌、协议状态机、量纲分析、验证边界、能力 mixin、幻影类型、const fn、Send/Sync——共十个模式章；Part III（[L24-L30](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L24-L30)）把它们组装成完整诊断平台，并给出 **Redfish 客户端与服务端两个实战走读**。本讲精读其中的能力令牌、协议状态机、幻影类型三章。

## 4. 核心概念与源码讲解

### 4.1 newtype 与 type-state：把「值」和「顺序」编码进类型

#### 4.1.1 概念说明

这是 rust-patterns-book 第 3 章的两个主角，分别解决两类「编译器本来帮不上忙」的错误：

- **newtype（新类型）**：用一个单字段元组结构体把既有类型包一层，制造出一个**全新的、不同的类型**。解决的问题是「两个语义不同的值碰巧同型」——`age: u32` 和 `employee_id: u32` 都是 `u32`，调换参数顺序照样编译通过；包成 `Age(u32)` 和 `EmployeeId(u32)` 之后，调换就变成编译错误。运行期开销为零。
- **type-state（类型状态）**：把对象「现在处于哪个状态」写进泛型参数，并且**每个状态下的可用方法只定义在对应的 `impl` 块里**。于是「在错误状态下调用方法」不是运行期 panic，而是 `no method named ...` 的编译错误——非法状态**无法表示**（unrepresentable）。

Expert 书第 1 章把这个思想整理成「三层正确性」，第 1 层「值正确性」正是 newtype 的哲学：私有字段的 `Port(u16)` 配合 `TryFrom` 校验，让 `Port(0)` 从构造入口就造不出来（[type-driven-correctness-book/src/ch01-the-philosophy-why-types-beat-tests.md:L36-L52](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch01-the-philosophy-why-types-beat-tests.md#L36-L52)）。该章开头还给出了动机总结：运行期检查的四种失败模式「只有在部署后才发现」，而「类型系统覆盖**所有**情况，包括没人想象过的那些」（[L22-L29](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch01-the-philosophy-why-types-beat-tests.md#L22-L29)）。

#### 4.1.2 核心流程

newtype 的工作流程：

```text
原始类型 T（如 u32）语义混乱
    │ 用 struct New(T) 包装
    ▼
新类型 New：与 T 是不同类型
    ├─ 混用 New 与 OtherNew → 编译错误（E0308 类型不匹配）
    └─ 需要内层数据时用显式方法 .0 或 as_ref() 暴露
```

type-state 的工作流程（对应章内 [rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L159-L169](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L159-L169) 的状态图）：

```text
定义零大小状态标记：struct Disconnected; struct Connected; struct Authenticated;
    │
结构体带状态参数：struct Connection<State> { address: String, _state: PhantomData<State> }
    │
每个状态一个专属 impl 块：
    impl Connection<Disconnected>  → new() / connect()
    impl Connection<Connected>     → authenticate()
    impl Connection<Authenticated> → request()
    │
转移方法签名固定为 fn transition(self, ...) -> Connection<下一状态>
    └─ 拿走 self（move）→ 旧状态的对象从此不存在 → 不可能「回到过去」
```

用状态-操作矩阵来看收益：设协议有 \( n \) 个状态、\( m \) 个操作，运行期检查方案要在 \( n \times m \) 个组合点上各写一次判断；type-state 方案只需 \( n \) 个 `impl` 块，矩阵中每个非法格子自动变成「方法不存在」，由编译器在**每个调用点**无条件检查。

#### 4.1.3 源码精读

先看 newtype 的最小对照示例，章节在 [rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L13-L28](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L13-L28)：这段代码先展示裸 `String`/`u32` 参数下「交换 age 和 id 也能编译」的隐患，再用 `UserName`/`Email`/`Age`/`EmployeeId` 四个 newtype 让同样的交换直接报 `expected Age, got EmployeeId`。

章节随后花了很大篇幅讲一个重要陷阱：给 newtype 实现 `Deref` 会「在抽象边界上凿一个洞」——内层类型的**所有**方法都自动可调用，任何不变量（比如「邮箱必须含 @」）都可能被 `.trim()`、`.split_at()` 之类的方法破坏（[L60-L62](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L60-L62)）。章末的决策矩阵给出简洁判定（[L147-L153](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L147-L153)）：只有「智能指针式、就是想要内层全部能力」的包装才该实现 `Deref`，为类型安全而生的 newtype 应改用显式委托。

再看本章核心 `Connection<State>` 例子。状态标记与结构体定义在 [L181-L190](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L181-L190)——三个零大小标记类型，加上一个用 `PhantomData<State>` 把状态参数挂进来的结构体：

```rust
struct Disconnected;
struct Connected;
struct Authenticated;

struct Connection<State> {
    address: String,
    _state: std::marker::PhantomData<State>,
}
```

`Disconnected` 状态专属的方法定义在 [L193-L208](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L193-L208)：`Connection<Disconnected>` 只有 `new()` 和 `connect()`，其中 `connect(self) -> Connection<Connected>` 消费旧值、返回新状态的值。同理 `Connected` 状态只有 `authenticate()`（[L210-L219](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L210-L219)），而 `request()` **只存在于** `impl Connection<Authenticated>` 块中（[L221-L226](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L221-L226)）。

`main` 函数（[L228-L238](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L228-L238)）把编译器的「执法过程」演示得很直观：在 `Disconnected` 或 `Connected` 状态下调用 `conn.request("/data")` 都被注释掉并标注 ❌，只有走完 `connect()` → `authenticate()` 之后（变量被重新绑定为新类型），`request` 才能编译。章节在 [L241-L243](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L241-L243) 点出关键洞察：每次转移消费 `self` 并返回新类型，旧状态转移后不可再用，且 `PhantomData` 零大小、状态在编译期即被擦除。

同一模式还能造出「强制填写必填字段的 builder」：`ServerConfig<NeedsName>` → `ServerConfig<NeedsPort>` → `ServerConfig<Ready>`，漏掉一步就没有 `build()` 方法可调（[L255-L328](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L255-L328)）；章末 Key Takeaways 把三个模式收成三句话（[L745-L748](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L745-L748)）。

#### 4.1.4 代码实践

这是一个「编译器当裁判」的观察型实践。

1. **实践目标**：亲眼看到 type-state 把「乱序调用」变成编译错误，而不是运行期异常。
2. **操作步骤**：
   - 本地运行 `cargo xtask serve`（或访问在线书站），打开 Rust Patterns 第 3 章，滚动到 `Connection<State>` 示例；该代码块是普通 ` ```rust ` 块，带 playground 编辑与运行按钮（u3-l2 讲过的配置在起作用）。
   - 点击编辑，把 `main` 中第一行注释 `// conn.request("/data");` 取消注释，点 Run。
   - 再把 `let conn = conn.connect();` 这一行注释掉，恢复上一条，再 Run 一次。
3. **需要观察的现象**：playground 的输出区不再是程序输出，而是编译诊断；两次实验的报错对象不同——一次是 `Connection<Disconnected>` 上没有 `request`，一次是 `Connection<Connected>` 上没有。
4. **预期结果**：两次都得到类似 `error[E0599]: no method named 'request' found for struct 'Connection<...>' in the current scope` 的错误，错误里的状态类型随你注释的转移步骤而变。具体措辞以 playground 实际输出为准（待本地验证）。
5. **注意**：本实践不修改仓库任何文件，全部改动都发生在浏览器 playground 里。

#### 4.1.5 小练习与答案

**练习 1**：`connect` 为什么签名是 `fn connect(self) -> Connection<Connected>`，而不是 `fn connect(&mut self)` 改内部状态？

**答案**：`&mut self` 方案下对象类型不变，`Disconnected` 状态的旧变量仍然存在，编译器无法阻止你在「已经 connect」之后再用旧接口，也无法阻止重复 connect。消费 `self` 后旧值已被 move，任何再用都是 `use of moved value` 错误；新状态是**新类型的值**，方法集合随之切换。

**练习 2**：`struct Age(u32)` 与直接用 `u32` 相比，运行期多付出什么代价？

**答案**：零。单字段元组结构体与内层类型布局一致（`repr(transparent)` 语义），优化器视其为同一个值；「代价」全部转移到编译期的类型检查上，这正是 newtype 被称为零成本抽象的原因。

**练习 3**：如果图省事给 `Email(String)` 实现 `Deref<Target = str>`，会引入什么风险？

**答案**：`Email` 会自动获得 `str` 的全部方法，`split_at`、`trim`、`replace` 都能直接调用，而这些操作不保证「含 @」的不变量；调用者拿到返回的 `&str` 再重新组装，校验就被绕过了。章节把它形容为在抽象边界上「凿洞」（[L60-L78](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L60-L78)），并建议改用显式委托或 `AsRef<str>`。

### 4.2 幻影类型：类型参数不占一个字节

#### 4.2.1 概念说明

4.1 的 `Connection<State>` 已经用到了 `PhantomData`，本模块把它扶正。**幻影类型（phantom type）**指出现在泛型参数列表、却不出现在任何真实字段里的类型参数——它纯粹携带类型层信息。`PhantomData<T>` 就是把它合法挂载到结构体上的标准工具。

为什么需要它？rust-patterns-book 第 4 章开篇给出定义：`PhantomData<T>` 是零大小类型，告诉编译器「这个结构体逻辑上关联了 `T`，虽然并不包含 `T`」，它影响型变（variance）、drop 检查与自动 trait 推断，却不占内存（[rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md:L9-L11](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md#L9-L11)）。该章把它的职责整理成三行表格（[L36-L42](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md#L36-L42)）：生命周期绑定（`PhantomData<&'a T>`）、所有权模拟（`PhantomData<T>`）、型变控制（`PhantomData<fn(T)>`）。

type-driven-correctness-book 第 9 章给出工程动机：硬件资源在代码里「长得一样但不可互换」——32 位寄存器和 16 位寄存器都是寄存器，读写 DMA 缓冲和只读 DMA 缓冲都是裸指针（[type-driven-correctness-book/src/ch09-phantom-types-for-resource-tracking.md:L7-L24](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch09-phantom-types-for-resource-tracking.md#L7-L24)）。幻影类型把「宽度」「方向」这类属性编码进类型，整类资源错配 bug 随之消失。

#### 4.2.2 核心流程

以寄存器宽度为例：

```text
定义宽度标记：Width8 / Width16 / Width32（零大小）
    │
句柄带幻影参数：struct Register<W> { base, offset, _width: PhantomData<W> }
    │
每个宽度一个 impl：impl Register<Width16> { read() -> u16 } ...
    │
工厂方法返回带正确标记的句柄：vendor_id() -> Register<Width16>
    │
效果：cfg.vendor_id().read() 只能赋给 u16；
      cfg.bar0().write(0u16) 直接编译错误（期望 u32）
```

与 4.1 的 type-state 相比，幻影类型标记的是「**这个值具有什么静态属性**」（宽度、方向、单位），而 type-state 标记的是「这个值处于生命周期哪一阶段」；两者机制同源（泛型参数 + 专属 impl 块），只是语义侧重不同。

#### 4.2.3 源码精读

[type-driven-correctness-book/src/ch09-phantom-types-for-resource-tracking.md:L31-L46](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch09-phantom-types-for-resource-tracking.md#L31-L46) 定义了四个宽度标记和带幻影参数的 `Register<W>`——注意真实字段只有 `base` 与 `offset`，宽度信息**只存在于类型里**：

```rust
pub struct Width8;
pub struct Width16;
// ...
pub struct Register<W> {
    base: usize,
    offset: usize,
    _width: PhantomData<W>,   // 零字节的编译期标记
}
```

接着每个宽度获得专属 `impl` 块，方法签名里的返回类型随之不同：`Register<Width16>::read` 返回 `u16`，`Register<Width32>::read` 返回 `u32`（[L48-L77](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch09-phantom-types-for-resource-tracking.md#L48-L77)）。`PcieConfig` 工厂按 PCIe 规范的偏移量发放带正确标记的句柄：`vendor_id()` 发 `Register<Width16>`，`bar0()` 发 `Register<Width32>`（[L79-L103](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch09-phantom-types-for-resource-tracking.md#L79-L103)）。最终 [L105-L114](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch09-phantom-types-for-resource-tracking.md#L105-L114) 的示例展示了两条被注释掉的「混用」调用及其编译错误——用 `u32` 接 `vendor_id().read()` 会得到 `expected u16`。

rust-patterns-book 第 4 章则展示了幻影参数更深的用法——**生命周期烙印**：`ArenaHandle<'arena>` 用 `PhantomData<*mut &'arena ()>` 把句柄与特定 arena 实例绑定，使「拿 A 仓库的句柄去 B 仓库取货」无法通过借用检查（[rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md:L44-L100](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md#L44-L100)）。这一技巧在 u3-l4 讲过的 Pin/自引用场景中同样关键。

#### 4.2.4 代码实践

源码阅读 + playground 验证。

1. **实践目标**：验证幻影标记确实「零字节」，并体会宽度错配的报错形态。
2. **操作步骤**：
   - 在书站打开 type-driven-correctness-book 第 9 章，把 `Register<W>` 定义（`base`、`offset`、`PhantomData`）与任一 `impl` 块抄进 playground，补一个 `fn main`，用 `std::mem::size_of::<Register<Width16>>()` 打印大小。
   - 再写 `let bad: u32 = cfg.vendor_id().read();`（需补上 `PcieConfig` 工厂）观察错误。
3. **需要观察的现象**：`size_of` 打印的数值等于两个 `usize` 字段之和（64 位平台上预期为 16），`PhantomData` 没有贡献任何字节；类型错配行报 `expected u16, found u32` 一类的 E0308 错误。
4. **预期结果**：零大小断言成立；错配行无法编译。`size_of` 的具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`Register<Width16>` 的 `_width: PhantomData<Width16>` 字段占几个字节？整个结构体呢？

**答案**：`PhantomData` 是零大小类型，字段占 0 字节；整个 `Register<Width16>` 只含 `base` 与 `offset` 两个 `usize`，大小与标记无关——这正是「零成本」的量化表述。

**练习 2**：为什么必须写 `PhantomData<W>`，直接 `struct Register<W> { base: usize, offset: usize }` 不行吗？

**答案**：不行。Rust 要求泛型参数必须被结构体使用，完全不使用 `W` 的定义会触发 `phantom data` 相关错误（unused parameter）；`PhantomData` 是「逻辑使用」这个参数的官方通道，同时让编译器正确处理型变与 drop 检查。

**练习 3**：幻影类型与 type-state 有什么异同？

**答案**：机制相同——零大小标记 + 泛型参数 + 按类型分派的专属 `impl` 块。语义不同——幻影类型标注的是值的**静态属性**（宽度、方向、单位），生命周期内不变；type-state 标注的是**协议阶段**，会通过消费 `self` 的转移方法变化。一本书用 `Register<Width16>`（属性），另一本用 `Connection<Authenticated>`（阶段），就是两种用法的对照。

### 4.3 能力令牌：函数签名即权限检查

#### 4.3.1 概念说明

type-driven-correctness-book 第 4 章的**能力令牌（capability token）**把「权限」也搬进类型系统：用一个零大小类型充当「调用者有权做危险操作」的**凭证**，危险函数要求按引用传入这个凭证。章首的问题陈述很直白：C/C++ 里每个危险函数都得自己写 `if (!bmc->is_admin)`，漏写一个就是提权漏洞（[type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md:L18-L30](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L18-L30)）。

令牌的不可伪造性靠两个手段：

1. **私有字段**：`AdminToken { _private: () }` 的字段私有，模块外无法用字面量构造它；
2. **唯一铸造入口**：只有模块内经过真正鉴权的 `authenticate_admin()` 才能返回令牌。

于是函数签名 `fn reset_pcie_link(&mut self, _admin: &AdminToken, ...)` 本身就是**证明义务（proof obligation）**——「想调用我，请先出示 AdminToken」，而出示令牌的唯一途径是走过鉴权流程。章节明确说这些令牌在编译产物里是零字节、只在类型检查期间存在（[L119-L122](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L119-L122)）。

#### 4.3.2 核心流程

```text
调用者                          模块（唯一的令牌铸造者）
   │                                │
   │  authenticate_admin(凭证)  ──▶ │ 校验凭证
   │◀── Ok(AdminToken) ──────────── │ （唯一能构造 AdminToken 的地方）
   │                                │
   │  reset_pcie_link(&admin, ...) ▶│ 签名要求 &AdminToken
   │                                │ → 编译器核对证明 → 放行
   │
   └─ 没有令牌就调用？编译错误：无法凭空产生 AdminToken 类型的值
```

延伸出三种变体（对应章内小节）：

| 变体 | 建模方式 | 解决的问题 |
|------|----------|-----------|
| 多步顺序 | 令牌链：`enable_standby()` 返回 `StandbyOn`，下一步要求 `&StandbyOn` | 上电时序不可颠倒（[L125-L185](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L125-L185)） |
| 权限层级 | trait 层级 `Admin: Operator: Authenticated` + 各级令牌实现 | 角色访问控制（[L187-L238](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L187-L238)） |
| 自动回收 | `ScopedAdminToken<'session>` 持有会话引用 | 令牌不能活得比会话久（[L240-L285](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L240-L285)） |

#### 4.3.3 源码精读

令牌定义与「唯一铸造入口」的约定见 [type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md:L41-L56](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L41-L56)：`AdminToken` 带 `_private: ()` 字段且注释声明它「不实现 Clone、不实现 Copy，必须显式传递」——不能复制意味着令牌不能被悄悄扩散。

`BmcController` 的三个方法构成完整闭环（[L58-L91](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L58-L91)）：

- `authenticate_admin()` 是**唯一**返回 `AdminToken` 的地方；
- `train_link()` 返回 `LinkTrainedToken`，证明链路已就绪；
- `reset_pcie_link(&mut self, _admin: &AdminToken, _trained: &LinkTrainedToken, slot)` 同时要求两种证明，方法体内**没有任何**权限判断。

随后的使用示例（[L97-L117](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L97-L117)）给出正反对照：`maintenance_workflow` 先取两枚令牌再重置链接，一切正常；`unprivileged_attempt` 只有 `trained` 没有 `admin`，注释里写着 `???`——那是一个**根本无法写出**的实参。章末的成本表（[L299-L306](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L299-L306)）总结：令牌 0 字节、传参被 LLVM 优化掉、trait 层级单态化为静态分发、生命周期只在编译期——整套权限模型运行期总开销为零。

#### 4.3.4 代码实践

源码阅读型实践：亲手体会「不可伪造」。

1. **实践目标**：确认令牌在模块外无法凭空构造。
2. **操作步骤**：
   - 在 playground 里新建两个模块：`mod bmc { pub struct AdminToken { _private: () } /* ... */ }` 与外面的 `fn main`。
   - 在 `main` 里尝试 `let t = bmc::AdminToken { _private: () };`。
   - 把 `AdminToken` 移到 `main` 同一模块再试一次。
3. **需要观察的现象**：跨模块构造时编译器报「字段 `_private` 是私有的」（E0603 类错误）；同模块内则可以构造。
4. **预期结果**：私有字段构成了模块边界上的「铸币厂围墙」——这正是书中 `authenticate_admin()` 必须与 `AdminToken` 同模块的原因。具体错误码待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`_private: ()` 这个字段本身是零大小的，它防住了什么？

**答案**：防的是**结构体字面量构造**。字段私有后，模块外写不出 `AdminToken { .. }`；而 `()` 又保证这个防护本身不占内存。防线是「可见性」而非「数据」。

**练习 2**：令牌不实现 `Clone`/`Copy` 有什么讲究？

**答案**：若可复制，任何拿到令牌的代码都能无限复印并扩散，权限模型的审计边界就失效了。不可复制迫使令牌按值/按引用显式传递，谁在什么时刻持有权限一清二楚。

**练习 3**：`ScopedAdminToken<'session>` 为什么不用运行期的「过期时间戳」检查？

**答案**：它持有 `&'session AdminSession`，借用检查天然保证令牌活不过会话——会话一旦移动或销毁，令牌的使用就是生命周期错误。这是「让编译器当门禁」的又一例：检查发生在编译期，零运行期成本。

### 4.4 协议状态机：type-state 用于真实硬件

#### 4.4.1 概念说明

type-driven-correctness-book 第 5 章是前三个模块的「会师之地」。硬件协议有严格状态机：IPMI 会话必须按「未认证 → 已认证 → 活跃 → 已关闭」推进，PCIe 链路训练要走「Detect → Polling → Configuration → L0」，乱序发命令轻则破坏会话、重则挂死总线（[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L9-L13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L9-L13)）。章内先用 Mermaid stateDiagram 画出两张协议状态图（IPMI 见 [L16-L27](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L16-L27)），再用一段 C 代码展示传统方案：enum 记状态 + 每个函数开头手写运行期检查，「很容易忘」（[L45-L63](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L45-L63)）。

type-state 方案的表述见 [L65-L70](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L65-L70)：每个协议状态是一个独立类型，转移是「消费一个状态、返回另一个状态」的方法；错误状态下调用方法之所以编译不过，不是因为检查失败了，而是因为**那个方法在那个类型上根本不存在**。

#### 4.4.2 核心流程

`IpmiSession<State>` 的状态机流程：

```text
IpmiSession<Idle>
    │ authenticate(user, pass)  消费 self，Result<..., String>
    ▼
IpmiSession<Authenticated>
    │ activate()                消费 self
    ▼
IpmiSession<Active>
    ├─ send_command(&mut self)  ✅ 只在此状态存在
    └─ close(self) ──────────▶ IpmiSession<Closed>（此后什么都做不了）
```

全章按「三拍（three beats）」递进，每拍叠加一种已学模式：

| 拍 | 协议 | 状态数 | 组合的模式 |
|----|------|:---:|-----------|
| 1 | IPMI 会话 | 4 | 纯 type-state |
| 2 | PCIe LTSSM | 5 | type-state + Recovery 分支（可回退重训） |
| 3 | 固件升级 | 6 | type-state + 能力令牌（4.3）+ 单次使用证明（ch03） |

表格源自 [L461-L471](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L461-L471)——到第三拍，编译器同时消灭三类 bug：状态乱序、权限不足、重复应用固件。

#### 4.4.3 源码精读

状态标记与结构体定义在 [type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L74-L88](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L74-L88)，注释强调状态「只存在于类型系统里」。`Idle` 状态的转移方法见 [L91-L112](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L91-L112)，`authenticate` 的签名 `pub fn authenticate(self, user: &str, pass: &str) -> Result<IpmiSession<Authenticated>, String>` 完整体现了「消费旧状态、返回新状态、可能失败」三要素。

最值得细读的是 `Active` 状态的 `impl` 块（[L128-L144](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L128-L144)）：`send_command` 内部直接 `self.session_id.unwrap()`，**敢裸 unwrap 是有类型层靠山的**——`session_id` 只在 `authenticate` 里被置为 `Some`，而只有走过 `authenticate` 才可能拿到 `Active` 状态的值，不变量由构造路径保证，无需运行期检查。随后的 `ipmi_workflow`（[L146-L168](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L146-L168)）在三处注释里演示了 Idle、Authenticated、Closed 状态下调用 `send_command` 的编译错误。章节小结一锤定音：「任何地方都没有运行期状态检查」，编译器负责认证先于激活、激活先于发命令、关闭后不可再发（[L171-L174](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L171-L174)）。

第三拍「固件升级」展示了模式组合：状态图（[L331-L345](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L331-L345)）里 `Verified` 状态旁注明「VerifiedImage 令牌被 apply() 消费」；代码中 `VerifiedImage` 与 `FirmwareAdminToken` 都用 `_private: ()` 封死外部构造（[L358-L365](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L358-L365)），而 `apply(self, proof: VerifiedImage)` 按值吃掉证明（[L417-L424](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L417-L424)）——工作流里第二次 `fw.apply(token)` 被注释为「use of moved value」（[L441-L458](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L441-L458)）。4.3 的能力令牌也在此回归：`firmware_update(&mut IpmiSession<Active>, &AdminToken, ...)` 的签名同时要求「会话活跃」与「管理员权限」两份证明，注释写道「签名就是检查」（[L287-L313](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L287-L313)）。

章末的适用性表格（[L473-L482](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L473-L482)）提醒分寸：IPMI、PCIe、TLS 握手、USB 枚举值得用 type-state；只有两个状态的简单请求/响应「大概不必」，无状态的消息收发「不要用」。Key Takeaways 第 6 条点出全书的落点：这套模式延伸为 ch17/ch18 的完整 **Redfish** 客户端与服务端实战（[L541-L548](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L541-L548)），对应 SUMMARY 的 Part III（[type-driven-correctness-book/src/SUMMARY.md:L27-L28](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L27-L28)）——服务器管理协议 Redfish 的会话生命周期与响应构造，正是本章 IPMI 例子的放大版。

> **源码阅读小发现**：本章 IPMI 代码块标记为 ` ```rust,ignore `（[L71](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L71)），且 [L76](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L76) 处有一行 `## Case Study: IPMI Session Lifecycle` 标题被误粘进代码栅栏内部——结合 u3-l2 讲过的三档代码块规则，可推断这就是该块只能「着色不可运行」的原因之一。读源码时留意这类排版痕迹，是判断「示例能否直接跑」的实用技巧。

#### 4.4.4 代码实践

1. **实践目标**：独立走通一次「模式组合」的阅读验证，为综合实践热身。
2. **操作步骤**：
   - 通读 [ch05 的固件升级工作流](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L441-L458)，对照 Mermaid 状态图（[L331-L345](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L331-L345)）。
   - 用纸笔为 `fw.apply(token)` 之后再加一行 `fw.apply(token)` 标注预期错误类别（提示：不再是「方法不存在」，而是所有权错误）。
   - 打开同书 `ch17-redfish-applied-walkthrough.md`（SUMMARY 第 13 项），找出它与本章 IPMI 例子在「会话状态」上的对应关系。
3. **需要观察的现象**：ch05 的工作流注释已经预演了你的答案（`use of moved value: token`）；ch17 的会话类型同样以状态参数化。
4. **预期结果**：你能画出一根「IPMI（4 状态）→ 固件（6 状态 + 双令牌）→ Redfish（生产规模）」的复杂度递进线。Redfish 章节的具体类型名待阅读时确认。

#### 4.4.5 小练习与答案

**练习 1**：`activate()` 里 `self.session_id.unwrap()` 为什么是安全的？

**答案**：`session_id` 唯一被置为 `Some` 的位置是 `authenticate()`，而能到达 `IpmiSession<Authenticated>` 的唯一路径就是 `authenticate()`——类型状态即构造历史，`Some` 这一不变量对所有 `Active`/`Authenticated` 值恒成立。这是「不变量由构造保证」的教科书示范。

**练习 2**：`firmware_update` 的签名要求哪两份证明？分别来自哪个模式？

**答案**：`&mut IpmiSession<Active>`（type-state：证明会话已按协议激活）与 `&AdminToken`（能力令牌：证明调用者有管理员权限）。签名即检查，函数体内无需任何判断。

**练习 3**：什么样的协议**不该**用 type-state？

**答案**：只有两个状态的简单请求/响应（状态信息太薄，模式收益抵不过样板代码），以及完全无状态的 fire-and-forget 消息（没有状态可编）。章末表格明确标注了这两类（[L481-L482](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L481-L482)）。

## 5. 综合实践

**任务：亲手造一扇「编译器看守的门」——`Door<Locked>` / `Door<Unlocked>`。**

这是本讲的贯通实践：用 4.1 的 type-state 骨架、4.2 的 `PhantomData` 挂载，让「未解锁就 `open()`」成为编译错误，并用 4.3 的思路体会「方法只存在于正确状态」。

**1. 实践目标**：不看书画出一个两状态 type-state 类型，并通过 `cargo check` 收集两类错误证据（方法不存在 E0599、值被移动后使用 E0382）。

**2. 操作步骤**：

仓库本身没有可运行 crate（这两本书的内容就是 Markdown），因此在仓库**外面**建一个练习项目（不要向仓库添加任何文件）：

```bash
cargo new /tmp/door-typestate
cd /tmp/door-typestate
```

把下面的示例代码写入 `src/main.rs`（**示例代码**，仿照 [rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L181-L238](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L181-L238) 的 `Connection` 与 [L1065-L1111](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L1065-L1111) 的红绿灯解写法）：

```rust
use std::marker::PhantomData;

// 状态标记：零大小类型
struct Locked;
struct Unlocked;

// 门，带状态参数；状态只存在于类型系统里
struct Door<State> {
    id: u8,
    _state: PhantomData<State>,
}

impl Door<Locked> {
    fn new(id: u8) -> Self {
        Door { id, _state: PhantomData }
    }

    // 只有上锁的门才能被解锁；转移消费 self
    fn unlock(self) -> Door<Unlocked> {
        println!("门 #{} 已解锁", self.id);
        Door { id: self.id, _state: PhantomData }
    }
}

impl Door<Unlocked> {
    // 只有解锁的门才能打开
    fn open(&self) {
        println!("门 #{} 已打开", self.id);
    }

    fn lock(self) -> Door<Locked> {
        println!("门 #{} 已上锁", self.id);
        Door { id: self.id, _state: PhantomData }
    }
}

fn main() {
    let door = Door::new(1);   // Door<Locked>
    // door.open();            // 实验一：取消注释
    let door = door.unlock();  // Door<Unlocked>
    door.open();               // ✅ 只有现在才合法
    let _door = door.lock();   // Door<Locked>

    // 实验二：把下面两行加入 main 末尾
    // let d1 = Door::new(2);
    // let d2 = d1.unlock();
    // let d3 = d1.unlock();   // d1 已被上一行移动
}
```

然后依次做两个实验，每个实验后运行 `cargo check` 并记录输出，再恢复原状：

- **实验一**：取消 `door.open();` 那行注释——在 `Door<Locked>` 上调用只存在于 `Door<Unlocked>` 的 `open`。
- **实验二**：在 `main` 末尾追加实验二的注释代码——对同一扇门**连续调用两次** `unlock`。第一次 `unlock(self)` 已经把 `d1` 移走，第二次再用就是所有权错误。这正是 rust-patterns-book 第 3 章能力表格里「Calling `unlock()` twice → value used after move」一行的复现（[对应表格](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L944-L951)）。

**3. 需要观察的现象**：`cargo run`（不做任何实验时）打印解锁、打开、上锁三条消息；实验一的错误指向「`Door<Locked>` 上没有 `open` 方法」；实验二的错误指向「`d1` 的值已被移动」。

**4. 预期结果**：

- 基线程序正常编译运行（待本地验证）。
- 实验一预期出现类似下面的诊断（措辞以本机 rustc 为准，待本地验证）：

```text
error[E0599]: no method named `open` found for struct `Door<Locked>` in the current scope
```

- 实验二预期出现 `error[E0382]: use of moved value: d1`——注意它与实验一的机制不同：E0599 是「这个方法在这个状态上不存在」，E0382 是「方法存在，但旧状态的值已经被第一次转移消耗掉了」。两类错误合起来，正好覆盖 type-state 的两道防线（方法集合 + 所有权）。

**5. 思考题（选做）**：给 `Door` 加一个 `alarm(&self)` 方法，要求它**只能在 Locked 状态**调用——你只需要把方法写进 `impl Door<Locked>` 块，其余什么都不用改。再想想：如果想让「开着的门不能重复 open」该怎么做？（提示：让 `open(self)` 也消费 `self`。）

## 6. 本讲小结

- **newtype** 用单字段包装制造新类型，零成本消灭「同型不同义」的参数错位；给不变量类型实现 `Deref` 会凿穿抽象边界，应改用显式委托。
- **type-state** 把协议阶段编码为泛型参数，每个状态的 `impl` 块只定义该状态合法的方法，转移方法消费 `self`——非法转移从运行期 panic 降格为「方法不存在」的编译错误。
- **幻影类型 / `PhantomData`** 让类型参数不占一个字节地携带「宽度、方向、生命周期烙印」等静态属性，是 type-state 与能力令牌共同的挂载机制。
- **能力令牌**用零大小类型 + 私有构造做不可伪造的权限凭证，函数签名即证明义务；层级能力用 trait 层级建模，作用域权限用生命周期回收。
- **协议状态机**是三者会师之处：IPMI → PCIe → 固件升级三拍递进，到 Redfish 实战时，编译器已同时看守状态顺序、操作权限与单次使用三类正确性。
- 两本书一个偏「工具箱」（patterns：从泛型到宏的 17 章地图），一个偏「方法论」（type-driven：围绕「让编译器证明正确性」的十个模式 + 实战），但底层是同一句话——**能写进类型的约束，就不要留给运行期**。

## 7. 下一步学习建议

- **继续 rust-patterns-book Part I 的剩余两章**：[ch01 Generics 全景](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L9-L9) 与 ch02 Traits 深入是本讲所有模式的语法地基（关联类型在 Config trait 模式中登场）；ch04 后半的型变（variance）值得与 u3-l4 的 Pin/Unpin 对照着读。
- **挑战双轴 typestate 与 Config trait**：回到 [rust-patterns-book 第 3 章后半](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L754-L754)（Dual-Axis 与 Config Trait 两节），看「厂商 × 状态」二维能力矩阵如何全部落进 `impl` 块。
- **走向 Expert 书的实战部分**：按 SUMMARY Part III 顺序读 ch12 诊断平台集成与 ch17/ch18 Redfish 走读（[SUMMARY.md:L24-L30](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L24-L30)），观察十个模式在同一个代码库里如何分工；第 16 章 Exercises 可作为自测。
- **与本系列其他讲义互参**：type-driven 第 11 章 Send/Sync 的编译期并发证明与本讲同源，可与 u3-l4 异步书中 `tokio::spawn` 的 `Send + 'static` 约束互相印证；patterns 第 12 章 unsafe 与第 13 章宏则在 u3-l6 工程书（Miri、验证工具）中有工具化延伸。
