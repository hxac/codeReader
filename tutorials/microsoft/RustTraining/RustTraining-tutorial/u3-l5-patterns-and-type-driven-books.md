# 高级与专家书：Patterns 与 Type-Driven Correctness

> **What you'll learn（本讲目标框）：**
> - 概览 `rust-patterns-book`（Advanced 级）与 `type-driven-correctness-book`（Expert 级）两本书的主题地图与互补关系
> - 理解 newtype、type-state、幻影类型（PhantomData）、能力令牌（capability token）如何把约束编码进类型
> - 亲手写出一个「让非法状态无法表示」的最小示例：`Door<Locked>` / `Door<Unlocked>`

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出两本书的分工：`rust-patterns-book` 讲**机制**（traits、associated types、type-state 怎么写），`type-driven-correctness-book` 讲**应用**（把这些机制用到 IPMI、PCIe、固件升级、Redfish 等真实领域）。
2. 解释核心设计思路：「把不变量从运行时检查推进类型系统，让编译器强制执行」——整类 bug 不是被测试发现，而是**根本无法编译**。
3. 独立写出 type-state 最小示例，并用 `cargo check` 验证「未解锁就开门」是编译错误（E0599），而不是运行时 panic。
4. 掌握四个互相配合的技巧：newtype 区分相似类型、PhantomData 携带零字节类型信息、能力令牌作为零成本权限证明、协议状态机把状态图编码进 `impl` 块。

## 2. 前置知识

本讲是内容层的高级主题，你需要先具备以下概念（不熟悉的部分建议先回看对应讲义）：

- **mdBook 章节范式**（u3-l2 已建立）：每章开头的 What you'll learn 目标框、难度 emoji（🟢 基础 / 🟡 中级 / 🔴 高级）、`rust,ignore` 注解（仅着色不可运行）、`<details>` 折叠的练习解答。本讲会直接沿用这些约定，不再重复解释。
- **书的体系定位**（u3-l1 已建立）：七本书分五级，`rust-patterns-book` 是 **Advanced（高级）**，`type-driven-correctness-book` 是 **Expert（专家）**——它们是三本桥梁书之后的两个专项深化方向。
- **Rust 语言基础**：泛型（`struct Foo<T>`）、trait 与关联类型、所有权移动语义（move）。两本书的引言都明确要求读者先掌握这些。
- **零大小类型（Zero-Sized Type, ZST）**：编译后不占任何字节的类型，如空结构体 `struct Locked;`。这是本讲所有技巧的「零成本」来源——类型信息只存在于编译期，运行时被完全擦除。
- **标记类型（marker type）**：不携带数据、只用于「在类型层面做记号」的类型。Rust 标准库的 `PhantomData<T>` 是最常用的标记载体。

一个直觉性的总纲（两本书反复强调的唯一原则）：

> **能用类型系统挡住的 bug，就不要留给运行时检查。** 运行时 `if state != ACTIVE { return -EINVAL }` 靠的是程序员记得写检查；类型系统挡住的是「这种调用根本写不出来」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rust-patterns-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L1-L42) | Advanced 书的目录：三个 Part 共 19 章，从类型级模式到并发再到系统与生产 |
| [rust-patterns-book/src/ch00-introduction.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch00-introduction.md#L1-L78) | 受众定位、难度图例、配速表（每章预计耗时与检查点） |
| [rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L1-L1119) | 本讲精读主力：newtype、type-state、builder、config trait、双轴 typestate |
| [rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md#L1-L260) | PhantomData 的三大职责：生命周期绑定、所有权模拟、型变控制 |
| [type-driven-correctness-book/src/SUMMARY.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L1-L35) | Expert 书的目录：四个 Part，核心模式 → 集成实战 → 参考 |
| [type-driven-correctness-book/src/ch00-introduction.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch00-introduction.md#L1-L80) | 两书关系的官方表述、「正确性谱系」图、按角色定制的阅读路径 |
| [type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L1-L240) | 能力令牌：零大小类型作为权限证明 |
| [type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L1-L551) | 协议状态机：IPMI 会话、PCIe 链路训练、固件升级三连示例 |
| [xtask/src/main.rs](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L35-L44) | 两本书在 BOOKS 注册表中的条目（category 分别为 `advanced` 与 `expert`） |

> 阅读提示：`type-driven-correctness-book` 的 SUMMARY 里，编号「12. Putting It All Together」指向的文件却是 `ch10-...md`，编号「10. Const Fn」指向 `ch15-...md`——文件名与显示编号并不对应。这再次印证了 u3-l1 的结论：**章节序号与导航完全由 SUMMARY 条目顺序决定，与文件名无关**。引用这两本书时请以 SUMMARY 为准。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：先建立两本书的地图（4.1），再按「newtype 与 type-state → 幻影类型 → 能力令牌 → 协议状态机」的顺序逐层深入——这恰好也是两本书自身的递进顺序。

### 4.1 两本书的主题地图与互补关系

#### 4.1.1 概念说明

`rust-patterns-book` 与 `type-driven-correctness-book` 是一对**配套书**，出自同一位作者（微软 SCHIE 硬件基础设施工程团队的 Principal Firmware Architect），但分工明确：

- **rust-patterns-book（Advanced）**：讲**机制**。它是一本「中级以上 Rust 模式手册」，覆盖泛型全景、trait 深入、newtype/type-state、PhantomData、通道与消息传递、闭包、智能指针与内部可变性、错误处理、序列化与零拷贝、unsafe、宏、测试与基准、crate 架构、async 入门。它回答「这个模式怎么写、什么时候用」。
- **type-driven-correctness-book（Expert）**：讲**应用**。它把上一本的机制套到真实领域——硬件诊断、密码学、协议校验、嵌入式系统——覆盖类型化命令接口、一次性类型、能力令牌、协议状态机、量纲分析、`Parse, Don't Validate`、能力 mixin、幻影类型资源追踪、`const fn`、`Send`/`Sync`，最终汇入 Redfish 客户端/服务端两个完整实战。

一句话区分：patterns 书教你**造工具**，type-driven 书教你**用工具造出「编译不了错误代码」的系统**。

#### 4.1.2 核心流程

两本书的推荐阅读流程是一个「漏斗」：

```text
读完任一桥梁书（掌握所有权、trait、泛型基础）
        │
        ▼
rust-patterns-book Part I（ch01 泛型 → ch02 trait → ch03 newtype/type-state → ch04 PhantomData）
        │  ←—— 机制层：四个类型级模式
        ▼
type-driven-correctness-book ch01（为什么类型胜过测试）
        │
        ▼
type-driven Part II 核心模式（ch02–ch09：按需选读）
        │
        ▼
Part III 集成实战（ch12 诊断平台 → Redfish client/server）
```

type-driven 书的引言甚至提供了一张**按角色定制**的路径表：IPMI/BMC 开发者走 ch02→ch05→ch07→ch10→ch17 约 2.5 小时；GPU/PCIe 开发者走另一条路；Redfish 实现者又是一条——这是一本被明确设计成「按需查阅」的参考书，而非线性教材。

#### 4.1.3 源码精读

**两书关系的官方表述**（type-driven 书引言第一段）：

[type-driven-correctness-book/src/ch00-introduction.md:L11-L13](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch00-introduction.md#L11-L13) —— 书的开篇直接点名与 patterns 书的分工："While the companion Rust Patterns book covers the **mechanics** ... this guide shows how to **apply** those mechanics to real-world domains"，并给出全书唯一原则：**push invariants from runtime checks into the type system so the compiler enforces them**（把不变量从运行时检查推进类型系统，让编译器强制执行）。

这种承接不是口头说说——type-driven 书的 Prerequisites 表逐条指回 patterns 书的具体章节：

[type-driven-correctness-book/src/ch00-introduction.md:L60-L68](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch00-introduction.md#L60-L68) —— 「Newtypes and type-state → Rust Patterns ch03」「PhantomData → Rust Patterns ch04」，前置知识表就是一张跨书依赖图。

**patterns 书的定位与配速**：

[rust-patterns-book/src/ch00-introduction.md:L11-L25](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch00-introduction.md#L11-L25) —— "A practical guide to intermediate-and-above Rust patterns ... This is not a language tutorial"：明确不是入门教程，假设你已会写基本 Rust；受众是「读完《The Rust Programming Language》但不知道怎么实际做设计的人」。

[rust-patterns-book/src/ch00-introduction.md:L43-L47](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch00-introduction.md#L43-L47) —— 配速表给 Part I 四章各分配 1–4 小时，并为每章设了一个「检查点」（如 ch03 的检查点是「能构建 type-state builder 模式」）。全书预计 30–45 小时。

**两本书的主题骨架**：

[rust-patterns-book/src/SUMMARY.md:L7-L35](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L7-L35) —— Part I「Type-Level Patterns」（ch01–ch04）是本讲的直接前置；Part II「Concurrency & Runtime」覆盖通道、线程、闭包、智能指针；Part III「Systems & Production」覆盖错误处理、序列化、unsafe、宏、测试、crate 架构与 async。

[type-driven-correctness-book/src/SUMMARY.md:L11-L30](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L11-L30) —— Part II「Core Patterns」列出了全书最核心的九个模式（含本讲精读的 ch04 能力令牌、ch05 协议状态机、ch09 幻影类型）；Part III「Integration & Practice」以诊断平台和 Redfish 实战收束。

**基础设施层的印证**——回到 xtask 的 BOOKS 注册表（承接 u1-l1 的「元数据双源」结论）：

[xtask/src/main.rs:L35-L44](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L35-L44) —— `rust-patterns-book` 注册为 `"advanced"`，`type-driven-correctness-book` 注册为 `"expert"`，与 u3-l1 的五级分类一致。顺带一个「漂移」实例：BOOKS 给 patterns 书的描述是 "Pin, allocators, lock-free structures, unsafe"，但实际目录覆盖面远比这四个词广（泛型、trait、错误处理、宏、测试都在书里）——落地页的一行描述滞后于内容本体，这正是 u1-l1 指出的「README 与 BOOKS 双源维护」会出现的语义漂移，只是这次发生在描述与真实内容之间。

#### 4.1.4 代码实践

1. **实践目标**：用两本书自带的「元数据」为自己规划一条阅读路径。
2. **操作步骤**：
   - 打开 [type-driven-correctness-book/src/ch00-introduction.md:L27-L35](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch00-introduction.md#L27-L35) 的配速表（Pacing Guide），找到最接近你背景的一行（如 "New to correct-by-construction" 或 "IPMI / BMC developer"）。
   - 对照 [rust-patterns-book/src/ch00-introduction.md:L43-L66](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch00-introduction.md#L43-L66) 的配速表，把 type-driven 路径中每章的前置（指回 patterns 书的章节）补进你的计划，形成一张「先读 patterns 哪几节、再读 type-driven 哪几章」的合并路线。
3. **需要观察的现象**：type-driven 的每条路径都跳过若干章节——它不是让你从头读到尾，而是按领域裁剪。
4. **预期结果**：一张标注了预估耗时（两表都给了小时数）的个人路线图。待本地验证（取决于你的背景选择）。

#### 4.1.5 小练习与答案

**练习 1**：type-driven 书的引言里，"correct-by-construction spectrum"（正确性谱系）把手段按安全性从低到高排成四级，是哪四级？

**答案**：运行时检查（runtime checks）→ 单元测试（unit tests）→ 属性测试（property tests）→ 构造即正确（correct by construction）。见 [type-driven-correctness-book/src/ch00-introduction.md:L70-L80](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch00-introduction.md#L70-L80)，谱系最右端给出的示例正是本讲的主角：`struct Celsius(f64);`——用类型本身消除「温度和转速混淆」这类 bug。

**练习 2**：如果你只想花 30 分钟快速了解 type-driven 书的全貌，引言推荐读哪两章？

**答案**：ch01（The Philosophy）+ ch13（Reference Card），即配速表中的 "Quick overview" 路径。参考卡章 [type-driven-correctness-book/src/SUMMARY.md:L34-L35](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L34-L35) 是全模式的目录与决策流程图。

### 4.2 newtype 与 type-state：把约束编码进类型

#### 4.2.1 概念说明

这是 patterns 书 Part I 的核心章（🟡 中级难度），解决两个层层递进的问题：

- **newtype**：用单字段元组结构体把既有类型包成**一个全新的类型**，零运行时开销地消除「参数顺序换错」类 bug。`struct Age(u32)` 和 `struct EmployeeId(u32)` 底层都是 `u32`，但编译器视其为两个不兼容的类型——把 `EmployeeId(42)` 传给期望 `Age` 的参数会直接编译失败。
- **type-state**：把对象可能处于的每个**状态**做成独立的类型，把状态迁移做成**消费旧值、返回新类型**的方法。于是「在错误状态下调用方法」不再可能——那个方法在错误的类型上**根本不存在**。书中反复引用的设计格言：让非法状态**不可表示**（unrepresentable）。

为什么需要它？对照你在 C/C#/Python 中的经验：一个网络连接必须先创建、再连接、再认证、最后才能发请求。传统写法用一个 state 枚举加运行时检查（`if !authenticated { throw }`），检查一旦漏写就是生产事故。type-state 把这套检查**搬进编译期**。

#### 4.2.2 核心流程

type-state 的机制可以用形式化的状态机语言描述。一个状态机：

\[ M = (S,\ s_0,\ \delta,\ F) \]

其中 \( S \) 是状态集，\( s_0 \) 是初始状态，\( \delta: S \times A \to S \) 是迁移函数。type-state 对它的编码规则：

- 每个状态 \( s \in S \) 对应一个零大小标记类型 \( T_s \)（如 `struct Disconnected;`）；
- 对象类型参数化为 `Machine<State>`，`PhantomData<State>` 让状态活在类型里而不占字节；
- 每条迁移边 \( \delta(s, a) = s' \) 对应一个方法 \( fn\ a(self) \to Machine<T_{s'} \)——**消费 `self`** 是关键，它保证迁移后旧状态无法继续使用；
- 初始状态由构造函数固定：`new()` 只存在于 `impl Machine<T_{s_0}>` 上；
- 只在状态 \( s \) 上合法的操作，只定义在 `impl Machine<T_s>` 里。

于是整个状态机的邻接表被逐行誊写进 `impl` 块的分布中，编译器做方法解析时就是在跑这个状态机：

```text
调用 conn.request("/data") 时编译器的方法解析：
  conn : Connection<Disconnected>
  在 impl Connection<Disconnected> 里找 request → 没找到
  在其他 impl 块里找 → request 定义在 impl Connection<Authenticated>，
     但 self 类型不匹配
  ⇒ error[E0599]: no method named `request`  ← 状态违规变成编译错误
```

#### 4.2.3 源码精读

**newtype 的动机示例**：

[rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L13-L28](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L13-L28) —— 左边是四个同类型参数（`name, email, age: u32, id: u32`）换错顺序也能编译的隐患代码；右边定义 `struct UserName(String); struct Email(String); struct Age(u32); struct EmployeeId(u32);` 后，`EmployeeId(42)` 传给 `Age` 参数变成编译错误。

**type-state 的状态图与定义**：

[rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L159-L171](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L159-L171) —— 用 mermaid `stateDiagram-v2` 画出 `Disconnected → Connected → Authenticated` 的合法迁移，并标注两条**不可能**的迁移（`Disconnected --request()--> ❌ won't compile`）；图下方的引言点明机制："Each transition *consumes* `self` and returns a new type — the compiler enforces valid ordering."

[rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L182-L190](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L182-L190) —— 三个零大小状态标记（`struct Disconnected; struct Connected; struct Authenticated;`）加上参数化结构体 `Connection<State> { address: String, _state: PhantomData<State> }`。注意 `_state` 字段是 `PhantomData`——它就是 4.3 节的主角。

[rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L193-L227](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L193-L227) —— 三个 `impl` 块分别只对一个状态开放方法：`Connection<Disconnected>` 才有 `new()` 和 `connect(self) -> Connection<Connected>`；`Connection<Connected>` 才有 `authenticate(self) -> Connection<Authenticated>`；`Connection<Authenticated>` 才有 `request()`。迁移方法全部拿 `self`（不是 `&self`），用完即耗散。

[rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L228-L238](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L228-L238) —— `main` 里两次被注释掉的 `conn.request("/data")` 分别演示在 `Disconnected` 和 `Connected` 状态下调用是编译错误，只有 `authenticate` 之后的那次调用合法。**这一段是普通 ` ```rust ` 代码块（非 ignore），意味着你在构建出的书页上可以直接点 playground 的运行/编辑按钮改写它**——u3-l2 讲过的「示例自包含 + 可运行」范式在这里兑现。

[rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L241-L245](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L241-L245) —— Key insight 块：迁移消费 `self`、旧状态不可复用、零运行时成本；并给出与 C++/C# 的对照——那边只能靠运行时检查（`if (!authenticated) throw ...`）。

本章还有三块延伸内容，本讲只指路不展开：

- **带 type-state 的 builder**（[L249-L329](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L249-L329)）：`ServerConfig<NeedsName> → <NeedsPort> → <Ready>`，强制「必填字段按序提供，缺一步就 `build()` 不了」。
- **config trait 模式**（[L422-L494](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L422-L494)）：把多个 trait 约束的泛型参数收敛进一个带关联类型的 `BoardConfig` trait，驯服「泛型参数爆炸」。
- **双轴 type-state**（[L754-L1054](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L754-L1054)）：`Handle<Vendor, State>` 用「厂商 × 状态」两个维度上的条件 `impl` 块编码一张能力矩阵——它预告了 type-driven 书把多个机制组合使用的思路。

章末的交通灯练习是本讲综合实践的模板：

[rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md:L1058-L1115](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L1058-L1115) —— 要求实现 `Red → Green → Yellow → Red` 的交通灯，其余顺序必须不可能；解答用 `<details>` 折叠（u3-l2 讲过的练习组织方式），核心结构与 `Connection` 例子完全同构。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到「在错误状态上调用方法」产生的编译器输出。
2. **操作步骤**（两条路线任选其一）：
   - **路线 A（零安装，推荐）**：按 u1-l3 跑起本地站点（`cargo xtask serve` 后访问 `http://localhost:3000/rust-patterns-book/`），进入第 3 章，找到 `Connection` 例子的代码块，点 playground 的**编辑**按钮，把 `fn main` 里 `let conn = conn.connect();` 之前的 `// conn.request("/data");` 注释去掉，点运行。
   - **路线 B（本地 Cargo）**：在仓库**之外**的目录（如 `/tmp/typestate-demo`，注意仓库根是 u2-l1 讲过的虚拟清单 workspace，不要在里面新建 crate）执行 `cargo new typestate-demo`，把书上 [L173-L238](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L173-L238) 的 `Connection` 例子抄进 `src/main.rs`，取消 L230 那行注释，运行 `cargo check`。
3. **需要观察的现象**：编译器报错，错误形如 `error[E0599]: no method named 'request' found for struct 'Connection<Disconnected>'`；运行时不会得到任何 panic——代码根本没活到运行阶段。
4. **预期结果**：一条 E0599 错误，错误信息会指出 `request` 存在于 `impl Connection<Authenticated>` 中。确切的措辞随 rustc 版本略有差异，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 type-state 的迁移方法必须写 `fn connect(self)` 而不是 `fn connect(&mut self)`？改成 `&mut self` 会失去什么保证？

**答案**：`self`（按值获取）会把旧值**移动**进方法，调用方从此无法再触碰旧状态；`&mut self` 只是可变借用，迁移完成后调用方手里仍握着旧状态的值，可以在 `Disconnected` 状态上再次调用别的方法，「迁移后旧状态不可用」的保证随之瓦解。type-driven 书 ch05 的 Key Takeaways 第 2 条正是这句话："Each transition consumes `self` — you can't hold onto an old state after transitioning"（[ch05:L541-L548](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L541-L548)）。

**练习 2**：本章对 newtype 实现 `Deref` 给出了强烈的警告。什么情况下 `Deref` 是反模式？

**答案**：当 newtype 存在的意义是**保护不变量或收窄 API** 时（如 `Email` 必须含 `@`、`Password` 要隐藏内容），`Deref` 会把内类型的全部方法漏出去，调用方可以绕过构造函数的校验直接操作内部值——相当于在抽象边界上凿了个洞；`DerefMut` 更会让外部直接改写内部值。只有当包装的意义是**透明地提供内类型全部能力**（智能指针、`String → str` 这类薄包装）时才适合 `Deref`。详见 [ch03:L64-L108](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L64-L108) 的两张对照表与决策矩阵。

**练习 3**：patterns 书的 Key Takeaways 给 newtype/type-state 各下了一句断语，是什么？

**答案**："Newtypes give compile-time type safety at zero runtime cost" 与 "Type-state makes illegal state transitions a compile error, not a runtime bug"，外加 "Config traits tame generic parameter explosion"。见 [ch03:L745-L748](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L745-L748)。

### 4.3 幻影类型 PhantomData：类型里不占字节的信息

#### 4.3.1 概念说明

`PhantomData<T>` 是标准库提供的**零大小**标记类型，用来告诉编译器「我这个结构体在逻辑上与 `T` 相关联，虽然我并没有真的存一个 `T`」。patterns 书第 4 章（🔴 高级）给它列了三个职责：

1. **生命周期绑定**（`PhantomData<&'a T>`）：声明结构体借用了 `'a` 的数据；
2. **所有权模拟**（`PhantomData<T>`）：让 drop 检查按「拥有一个 T」对待结构体；
3. **型变控制**（`PhantomData<fn(T)>` 等）：决定结构体对 `T` 是协变、逆变还是不变。

对 4.2 的 type-state 而言，它还有第四个更基础的作用：**Rust 要求泛型参数必须被使用**。`struct Connection<State> { address: String }` 里 `State` 没出现在任何字段中，编译器会拒绝（`parameter State is never used`）；加上 `_state: PhantomData<State>` 既满足规则，又不占一个字节。可以说：**PhantomData 是把「类型信息」从字段中解耦出来的桥梁——类型系统需要知道，内存布局不需要付出**。

#### 4.3.2 核心流程

`PhantomData` 在 type-state 里的工作回路：

```text
定义侧：struct Door<State> { _state: PhantomData<State> }
                │
                │  编译期：State 参与类型检查（Door<Locked> ≠ Door<Unlocked>）
                │  编译期：size_of::<PhantomData<Locked>>() == 0
                ▼
使用侧：door.unlock() 返回 Door<Unlocked>
                │
                ▼
产物：单态化后 Locked/Unlocked 被擦除，Door<Locked> 与
      Door<Unlocked> 的内存布局完全相同 —— 零运行时成本
```

书中还有两个「不用 PhantomData 参数、只用零大小标记类型」的兄弟模式值得知道：**量纲模式**（unit-of-measure，`Quantity<Meters>` 与 `Quantity<Seconds>` 不能相加）和**生命周期烙印**（lifetime branding，不同 arena 的句柄不能混用）。

#### 4.3.3 源码精读

[rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md:L9-L34](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md#L9-L34) —— 开篇的 `Slice<'a, T>` 对照：裸写 `ptr + len` 时编译器不知道结构体借用了 `'a`；加 `_marker: PhantomData<&'a T>` 后借用关系、协变性、drop 检查全部就位。

[rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md:L36-L42](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md#L36-L42) —— 「三职责」速查表，一表浓缩上一小节的 1、2、3。

[rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md:L104-L157](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md#L104-L157) —— 量纲模式：`struct Quantity<Unit> { value: f64, _unit: PhantomData<Unit> }` 配合为 `Add`/`Div` 手写的 `impl`，让 `Quantity<Meters> + Quantity<Seconds>` 编译失败、`Meters / Seconds` 自动得到 `Quantity<MetersPerSecond>`。书末点题：`Quantity<Meters>` 的内存布局与裸 `f64` 完全相同——"pure type-system magic"。

[rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md:L44-L102](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md#L44-L102) —— 生命周期烙印：`ArenaHandle<'arena>` 用 `PhantomData<*mut &'arena ()>`（对 `'arena` 不变）把每个句柄烙上所属 arena 的印记，拿 A arena 的句柄去查 B arena 直接编译失败。

本章后半部（L159 起）进入型变（variance）的深水区——协变/逆变/不变如何由 `PhantomData` 的类型参数决定，属于 🔴 高级内容，本讲只指路。

#### 4.3.4 代码实践

1. **实践目标**：亲手触发「泛型参数未使用」错误，理解 `PhantomData` 在 type-state 里存在的必要性。
2. **操作步骤**：在 4.2.4 路线 B 的 `/tmp/typestate-demo/src/main.rs` 里，把 `Connection` 结构体的 `_state: PhantomData<State>` 字段**整行删掉**（同时删掉各处构造它的 `_state: PhantomData` 行），运行 `cargo check`；随后还原。
3. **需要观察的现象**：编译器报出类似 `error[E0392]: parameter 'State' is never used` 的错误，并建议考虑使用 `PhantomData`。
4. **预期结果**：E0392 错误一条；还原后恢复编译通过。确切措辞随版本略有差异，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`Quantity<Meters>` 和裸 `f64` 在运行时有什么区别？

**答案**：没有任何区别——`PhantomData<Meters>` 大小为 0，`Quantity<Meters>` 的布局就是 `f64`。区别全部在编译期：前者只能与同量纲相加、除法产生正确的新量纲类型。这就是「零成本抽象」在这两个模式中的字面含义。

**练习 2**：写一个容器拥有自己的数据，`PhantomData` 该怎么选？写一个只指向数据的视图类型呢？

**答案**：拥有数据的容器用 `PhantomData<T>`（drop 检查会假定可能 drop 一个 `T`，要求 `T` 活得比容器久）；视图/引用类型用 `PhantomData<&'a T>` 或 `PhantomData<*const T>`（不宣称所有权）。见 [ch04:L159-L184](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch04-phantomdata-types-that-carry-no-data.md#L159-L184) 的对照代码与实用规则。

### 4.4 能力令牌：零大小的权限证明

#### 4.4.1 概念说明

进入 Expert 书。能力令牌（capability token，type-driven 书 ch04，🟡）回答的问题是：**谁被允许做什么**。

硬件诊断里有些操作是危险的——刷 BMC 固件、复位 PCIe 链路、写 OTP 熔丝。C/C++ 的做法是每个危险函数开头写运行时权限检查，漏写一个就是提权漏洞。能力令牌的方案：

- 定义一个**零大小类型**作为「权限证明」，比如 `AdminToken`；
- 它**只有一个合法的构造入口**（如 `authenticate_admin()`），字段私有（`_private: ()`）使模块外无法字面量构造；
- 不实现 `Clone`/`Copy`——证明必须显式传递，不能复制；
- 危险函数把令牌写进签名：`fn reset_pcie_link(&mut self, _admin: &AdminToken, ...)`。

于是**函数签名本身就是检查**（书中称之为 proof obligation，证明义务）：编译期你拿不出 `AdminToken`，就写不出这个调用。而令牌是零字节的——「zero-cost proof of authority」（零成本的权限证明）这一章副标题是字面属实的。

它与 type-state 的分工：type-state 证明「**东西**处在什么状态」，能力令牌证明「**调用者**有什么权限」——4.5 节会看到两者组合。

#### 4.4.2 核心流程

```text
传统运行时检查                     能力令牌
──────────────                    ──────────
fn reset(link, slot) {            fn reset(&mut self,
  if (!is_admin)      ← 忘写即漏洞      _admin: &AdminToken,   ← 签名即检查
    return -EPERM;                    _trained: &LinkTrainedToken,
  if (!link_trained)  ← 又一个           slot: u32)
    return -EINVAL;                  // 函数体内零检查
  ...                               }
}
                                  唯一入口：authenticate_admin() → AdminToken
                                  编译失败点：拿不出令牌 = 调用无法通过类型检查
```

多个令牌可以叠加成**时序证明**：电源时序（standby → auxiliary → main → CPU）中，每一步返回下一步要求的令牌，跳步的调用因造不出所需令牌而无法编译。

#### 4.4.3 源码精读

[type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md:L20-L30](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L20-L30) —— 问题陈述的 C 版本：`reset_pcie_link` 里两个运行时检查（`is_admin`、`link_trained`），书点评「每个危险函数都要重复这些检查，忘掉一个就是提权 bug」。

[type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md:L47-L54](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L47-L54) —— 两个令牌的定义：`pub struct AdminToken { _private: () }` 与 `LinkTrainedToken`，注释点明三件事——零大小、编译后完全消失、非 Clone 非 Copy 必须显式传递；私有字段 `_private` 阻止模块外构造。

[type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md:L82-L90](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L82-L90) —— `reset_pcie_link` 的签名同时要求 `&AdminToken`（权限证明）与 `&LinkTrainedToken`（状态证明），函数体一行检查都不写——"No runtime checks needed — the tokens ARE the proof"。

[type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md:L96-L122](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L96-L122) —— 用法对照：`maintenance_workflow` 先认证拿令牌、再训练拿令牌、最后复位，三步全过；`unprivileged_attempt` 没有管理员令牌，`reset_pcie_link(???, ...)` 处直接写不出实参。L119-L122 总结：令牌在编译产物里是零字节，只活在类型检查期间——签名就是证明义务，而产出证明的唯一途径是通过认证函数。

[type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md:L124-L185](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L124-L185) —— 电源时序案例：`StandbyOn`/`AuxiliaryOn`/`MainOn`/`CpuPowered` 四个令牌串成链，`enable_auxiliary` 必须吃 `&StandbyOn`——「反序可能损坏硬件」的约束由编译器看守。

本章后半（L187 起）还有层级能力（用 trait 继承 `Operator: Authenticated`、`Admin: Operator` 表达「管理员能做用户的一切再加更多」）与带生命周期的可撤销令牌，指路不展开。

#### 4.4.4 代码实践

1. **实践目标**：验证「模块外造不出令牌」这一保证。
2. **操作步骤**：新建 `/tmp/cap-demo`（`cargo new cap-demo`），把书上的 `AdminToken`/`BmcController` 精简版抄入，并把 `BmcController` 与令牌放在 `mod auth { ... }` 里、`main` 放在模块外；然后在 `main` 里尝试 `let t = auth::AdminToken { _private: () };`，运行 `cargo check`。
3. **需要观察的现象**：编译器报错——`_private` 字段是私有的，模块外无法构造 `AdminToken`；你只能调 `authenticate_admin()` 拿令牌。
4. **预期结果**：一条关于私有字段不可访问（E0616）的错误。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `AdminToken` 要刻意不派生 `Copy`/`Clone`？如果派生了会破坏什么？

**答案**：不派生使令牌成为**必须显式移动/传递**的对象，调用链上每一环都看得见权限的流向；一旦可复制，任何拿到引用的人都能私自复制一份永久留底，权限的授予范围失去控制。同章 ch03（single-use types）更是反向利用移动语义：令牌被消费后不可再用，`fw.apply(token)` 第二次调用会报 use of moved value。

**练习 2**：能力令牌与 type-state 各证明什么？两者的单位成本分别是多少？

**答案**：type-state 证明**对象**当前所处状态（`IpmiSession<Active>`），能力令牌证明**调用方**拥有的权限（`AdminToken`）；两者都是零大小类型，运行时成本均为零，只存在于类型检查期。见 ch05 的组合示例（4.5 节）。

### 4.5 协议状态机：type-state 在真实硬件上的应用

#### 4.5.1 概念说明

type-driven 书 ch05（🔴 高级）是 type-state 的「实战篇」，标题直译是「协议状态机——为真实硬件而生的 type-state」。硬件协议都有严格的状态机：IPMI 会话要走 `Idle → Authenticated → Active → Closed`，PCIe 链路训练要走 `Detect → Polling → Configuration → L0`；在错误状态发命令会毁掉会话或挂死总线。

本章的结构是三个难度递增的「beat」（节拍）：

| Beat | 协议 | 状态数 | 组合了什么 |
|:----:|------|:------:|-----------|
| 1 | IPMI 会话 | 4 | 纯 type-state |
| 2 | PCIe LTSSM | 5 | type-state + Recovery 回退分支 |
| 3 | 固件升级 | 6 | type-state + 能力令牌（ch04）+ 一次性证明（ch03） |

到 beat 3，编译器同时强制三件事：状态顺序、管理员权限、镜像只能应用一次——三类 bug 在一个状态机里同时被消灭。这章也是「专家书」的典型切片：单个机制不新（patterns ch03 已教），新的是**把机制组合起来贴合真实协议**。

#### 4.5.2 核心流程

以 beat 1 的 IPMI 会话为例，状态机 \( M = (S, s_0, \delta, F) \) 的编码映射：

- \( S \)：`{Idle, Authenticated, Active, Closed}` → 四个零大小结构体；
- \( s_0 = \text{Idle} \)：`new()` 只定义在 `impl IpmiSession<Idle>`；
- \( \delta \)：`authenticate(self) -> ...<Authenticated>`、`activate(self) -> ...<Active>`、`close(self) -> ...<Closed>`，每条边一个消费 `self` 的方法；
- 合法动作的**定义域**即状态：`send_command` 只存在于 `impl IpmiSession<Active>`，等价于状态机中「`send_command` 仅在 Active 状态有自环」这条标注。

C 与 Rust 的对照是本章最有教学价值的一组截图：

```text
C 版本（运行时）：                       Rust 版本（编译期）：
enum {IDLE, AUTH, ACTIVE, CLOSED};      pub struct Idle; pub struct Authenticated;
if (s->state != ACTIVE) {               impl IpmiSession<Active> {
    return -EINVAL;   ← 忘写即事故          pub fn send_command(...)  ← 别处不存在
}                                       }
```

#### 4.5.3 源码精读

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L16-L27](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L16-L27) —— IPMI 状态机的 mermaid 图，两个 note 直接把结论画进图里：`send_command() only exists here`（Active）与 `Idle 状态下 send_command() → compile error`。

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L45-L63](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L45-L63) —— C 对照版：枚举加 `if (s->state != ACTIVE)` 的运行时检查，注释自评 "runtime check — easy to forget"。

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L74-L88](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L74-L88) —— Rust 版的状态标记与参数化结构体 `IpmiSession<State>`，注释点明 "The state exists ONLY in the type system (PhantomData is zero-sized)"——这正是 4.3 节 PhantomData 的用武之地。（顺带一个原文档小瑕疵：L76 行「## Case Study: IPMI Session Lifecycle」误落在代码围栏内部，导致这行标题在渲染页上显示为代码而非标题——一个适合 u4-l5 贡献流程练手的真实修复点。）

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L91-L144](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L91-L144) —— 三个 `impl` 块逐状态开放方法：`Idle` 上是 `new()` 和 `authenticate(self) -> Result<...<Authenticated>, String>`（认证可能失败，所以返回 `Result`）；`Authenticated` 上是 `activate()`；`Active` 上才有 `send_command(&mut self, ...)` 与 `close(self)`。注意 `activate` 里那句注释：`session_id` 在 `Active` 状态**由类型保证是 `Some`**——不变量被迁移路径固化，`unwrap` 不再是赌博。

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L146-L175](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L146-L175) —— `ipmi_workflow` 串起全程：三处被注释的 `send_command` 分别展示在 `Idle`、`Authenticated`、`Closed` 状态下调用全部是编译错误；收尾总结 "No runtime state checks anywhere"，编译器强制了认证先于激活、激活先于发命令、关闭后不可再发。

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L287-L322](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L287-L322) —— 机制组合的关键一跳：`firmware_update(session: &mut IpmiSession<Active>, _admin: &AdminToken, image)` 同时要求 Active 会话（type-state）与管理员令牌（能力令牌），注释点题 "the signature IS the check"。调用方必须依次完成认证、激活、取得令牌五步，全部在编译期强制、零运行时成本。

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L350-L370](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L350-L370) —— beat 3 的固件升级状态机：六个状态标记、一次性证明类型 `VerifiedImage`（私有字段防伪造、携带摘要）、能力令牌 `FirmwareAdminToken`、参数化结构 `FwUpdate<S>` 三件套齐上。

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L417-L424](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L417-L424) —— 组合的收口：`apply(self, proof: VerifiedImage)` 按值吃掉一次性证明，注释写明 "proof is moved — can't be reused"——验证过的镜像**在类型层面无法被应用第二次**。

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L461-L482](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L461-L482) —— 三个 beat 的小结表 + 「何时该用 type-state」决策表。后者同样重要：只有 2 个状态的简单请求/响应（⚠️ 大概不必）、无状态的 fire-and-forget（❌ 不用）就不要上 type-state——专家书的克制也是课程的一部分。

[type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md:L541-L549](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L541-L549) —— 章末 Key Takeaways 六条，首尾两条值得背下来：错误顺序的调用**不可能**（方法在错误状态上不存在）；不要过度使用（两状态协议用 type-state 反而更复杂）。最后一条预告 ch17/ch18 把该模式延伸到 Redfish 全流程。

> 一个值得留意的注解细节：本章所有大代码块都用 ` ```rust,ignore ` 标注（u3-l2 讲过：仅着色、不可在 playground 运行），部分段落还用了 `# use std::marker::PhantomData;` 这类以 `#` 开头的隐藏行（rustdoc 文档测试语法）。这与 patterns 书 ch03 的可运行 ` ```rust ` 块形成对照——章节作者会按内容性质选择注解，读源码时它能提示你「这段是否被自动验证过」。

#### 4.5.4 代码实践

1. **实践目标**：独立完成书上留的 USB 枚举练习，再对照官方解答。
2. **操作步骤**：
   - 阅读练习题 [ch05:L484-L486](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L484-L486)：建模 USB 设备的 `Attached → Powered → Default → Addressed → Configured` 五态迁移，每步消费前一状态，`send_data()` 仅在 `Configured` 可用。
   - **先不看解答**，在 `/tmp/usb-demo` 自己写（结构与 4.2 的 `Connection` 完全同构，五个状态五个 `impl` 块）。
   - 写完展开 [L488-L539](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L488-L539) 的折叠解答对照。
3. **需要观察的现象**：你的版本能否做到「跳过 `set_address` 直接 `configure`」编译不过；官方解答用了哪些你没想到的简化。
4. **预期结果**：一个约 40 行的可编译程序，`cargo check` 通过；故意写错顺序时得到 E0599。

#### 4.5.5 小练习与答案

**练习 1**：beat 3 的固件升级中，「防止同一镜像被应用两次」是由哪个机制实现的——type-state、能力令牌还是一次性类型？

**答案**：一次性类型（single-use types，ch03）。`verify_ok` 同时返回 `(FwUpdate<Verified>, VerifiedImage)`，而 `apply(self, proof: VerifiedImage)` 按值消费证明；第二次 `apply(token)` 因 `token` 已被移动而报 use of moved value。type-state 管顺序、能力令牌（`FirmwareAdminToken`）管「只有管理员能开始上传」，三者各司其职——见 [ch05:L461-L471](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L461-L471) 的组合表。

**练习 2**：PCIe LTSSM 的例子比 IPMI 多了什么结构性难点，书上如何处理？

**答案**：多了**回退/重训分支**——`L0` 可经 `enter_recovery()` 进入 `Recovery`，`Recovery` 再经 `retrain(speed)` 回到 `L0`（也可能失败回 Detect）。处理方式仍是老规矩：每个状态一个 `impl` 块，`Recovery` 的 `impl` 里提供 `retrain` 方法，编译器因此自动认可 `L0 → Recovery → L0` 循环、拒绝从 `Detect` 直接 `send_tlp`。见 [ch05:L235-L263](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L235-L263)。

**练习 3**：`activate()` 里的 `self.session_id.unwrap()` 为什么是安全的？

**答案**：因为到达 `IpmiSession<Authenticated>` 的唯一路径是 `authenticate()`，而它已经把 `session_id` 置为 `Some`——不变量由类型迁移链保证，`unwrap` 不会失败。这正是「把运行时断言升级为类型证明」的红利：注释原话 "session_id is guaranteed Some by the type-state transition path"（[ch05:L115-L125](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch05-protocol-state-machines-type-state-for-r.md#L115-L125)）。

## 5. 综合实践

把本讲四个模块串成一个任务：**实现一个「未解锁就开门无法编译」的门锁 type-state**，并叠加一个能力令牌。

**任务描述**：门只有 `Locked`/`Unlocked` 两个状态。`open()` 只对解锁的门可用；`lock()` 只对解锁的门可用；`unlock()` 需要住户令牌（能力令牌）才能调用——没有令牌连门都开不了。

以下为示例代码（本讲义新写，仿照 [rust-patterns-book ch03 的 Connection 范式](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L182-L238)与 [type-driven ch04 的令牌范式](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/ch04-capability-tokens-zero-cost-proof-of-aut.md#L47-L54)）：

```rust
use std::marker::PhantomData;

// ── 能力令牌（4.4）：零大小、私有字段防外部构造、不可 Clone ──
pub struct ResidentToken { _private: () }

pub struct Building {
    // 真实系统中这里会有认证逻辑
}

impl Building {
    // 产出令牌的唯一入口
    pub fn authenticate(&mut self, _key: &str) -> ResidentToken {
        ResidentToken { _private: () }
    }
}

// ── type-state（4.2/4.5）：状态即类型 ──
pub struct Locked;
pub struct Unlocked;

pub struct Door<State> {
    room: &'static str,
    _state: PhantomData<State>,   // ← 4.3：幻影类型，零字节
}

impl Door<Locked> {
    pub fn new(room: &'static str) -> Self {
        Door { room, _state: PhantomData }
    }

    // 解锁需要令牌（能力令牌 × type-state 组合）
    pub fn unlock(self, _resident: &ResidentToken) -> Door<Unlocked> {
        println!("🔓 {room} 解锁", room = self.room);
        Door { room: self.room, _state: PhantomData }
    }
}

impl Door<Unlocked> {
    pub fn open(&self) {
        println!("🚪 {room} 开门", room = self.room);
    }

    pub fn lock(self) -> Door<Locked> {
        println!("🔒 {room} 上锁", room = self.room);
        Door { room: self.room, _state: PhantomData }
    }
}

fn main() {
    let mut bldg = Building;
    let resident = bldg.authenticate("secret-key");

    let door = Door::new("A-101");            // Door<Locked>
    // door.open();                           // ① 编译错误：Locked 上没有 open
    let door = door.unlock(&resident);        // Door<Unlocked>
    door.open();                              // ✅
    let door = door.lock();                   // 回到 Door<Locked>
    // door.open();                           // ② 编译错误：再次上锁后

    // 没有令牌的解锁：
    // door.unlock(???);                      // ③ 编译错误：造不出 ResidentToken
}
```

**操作步骤**：

1. 在仓库外执行 `cargo new /tmp/door-demo && cd /tmp/door-demo`（仓库根是虚拟清单 workspace，勿在其中建 crate——u2-l1 的结论）。
2. 把上面的代码放进 `src/main.rs`，先原样 `cargo run`，应看到解锁→开门→上锁三条输出。
3. 依次取消 ①②③ 三行注释，每次只放开一行，运行 `cargo check` 记录错误。
4. 进阶：把 `open(&self)` 改成 `open(self)` 试试消费语义，或参考 [patterns ch03:L249-L329](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/ch03-the-newtype-and-type-state-patterns.md#L249-L329) 的 builder 模式给门加一个 `Emergency`（消防通道常开）状态。

**需要观察的现象**：

- ①② 两处是 `error[E0599]: no method named 'open' found for struct 'Door<...>'`——**门的状态错误**被 type-state 拦下；
- ③ 处是你根本无法构造 `ResidentToken`——**权限缺失**被能力令牌拦下（若在模块外还会先撞上私有字段 E0616）；
- 程序正常路径零运行时检查、零字节额外开销。

**预期结果**：三条 E0599/E0616 类编译错误按预期出现，还原后程序运行输出正常。确切的错误措辞随 rustc 版本略有差异，待本地验证。

## 6. 本讲小结

- 两本书是一对配套：`rust-patterns-book`（Advanced）讲**机制**，`type-driven-correctness-book`（Expert）把机制**应用**到硬件诊断、协议校验等真实领域；后者引言的原则一句话概括——把不变量从运行时检查推进类型系统。
- **newtype** 用单字段包装零成本区分相似类型；**type-state** 把状态机的每个状态编码成类型、每条迁移写成消费 `self` 的方法，让非法状态迁移变成编译错误（E0599）而不是运行时 bug。
- **PhantomData** 是零大小的标记载体，让类型信息参与编译检查而不占任何内存，同时承担生命周期绑定、所有权模拟、型变控制三项职责。
- **能力令牌**是零大小、不可复制、只有一个构造入口的权限证明；「函数签名即检查」，拿不出令牌就写不出调用。
- **协议状态机**把上述机制组合起来贴合真实协议（IPMI、PCIe、固件升级）：状态顺序、管理员权限、一次性消费三类 bug 在同一个状态机里同时被编译器消灭——但决策表也提醒：两状态的简单协议不必上 type-state。
- 基础设施层侧面印证：xtask 的 BOOKS 注册表把两本书分别标为 `advanced` 与 `expert`，且其一行描述已滞后于书的实际内容——「双源维护」的漂移在描述与内容之间同样存在。

## 7. 下一步学习建议

- **下一讲 u3-l6（工程实践书）**：`engineering-book` 覆盖 build.rs 深入、交叉编译、Miri 与消毒器、生产 CI/CD——它是 Practices 级的收官，与本书「编译期证明」互补的是「工具链期的验证」。
- **继续深挖 patterns 书**：本讲只精读了 Part I 的 ch03/ch04；第 4 章后半的型变（variance）与 [ch12 unsafe](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/rust-patterns-book/src/SUMMARY.md#L26-L35) 值得单独安排。
- **type-driven 书的收束章**：[ch17/ch18 的 Redfish 实战](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L24-L30)把八个模式组合进一个类型安全的 Redfish 客户端/服务端，是「学完就找工作场景」的落点；[ch14 Testing Type-Level Guarantees](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/type-driven-correctness-book/src/SUMMARY.md#L32-L35) 教你用 trybuild 把「应该编译不过的代码」变成测试断言——本讲综合实践的手工验证可以升级成自动化测试。
- **动手方向**：把综合实践的 `Door` 扩展成三态（加 `Jammed`），或给自己项目里的某个真状态机（连接池、事务、订单流）做一次 type-state 改造，用 `cargo check` 感受「整类 bug 消失」。
