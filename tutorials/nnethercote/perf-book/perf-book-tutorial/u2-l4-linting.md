# Linting——用 Clippy 自动发现性能问题

## 1. 本讲目标

本讲精读 perf-book 第 4 章「Linting」。读完后你应该能够：

- 理解 **Clippy** 是什么、它如何用「lint（静态模式检查）」自动发现常见错误，以及为什么「自动化检测优于手工检测」；
- 学会运行 `cargo clippy`，并通过官方 lint 列表筛选出 **Perf 性能 lint 组**；
- 理解「性能收益」是双向的——**Perf 组 lint 让代码又快又简洁**，而**某些非 Perf lint（如 `ptr_arg`）也能顺带提速**；
- 掌握用 Clippy 的 `disallowed_types` lint 配合 `clippy.toml`，**统一禁用标准库里偏慢的类型**（如 `HashMap`/`HashSet`），防止团队误用；
- 把 Clippy 放进前面几讲建立的优化工作流里：它是一种**几乎零成本、可在写代码时就跑**的检测，是基准测试（u2-l1）与性能剖析（u2-l2）之外的第一道防线。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，什么是 lint。** 在编译器里，「lint」是一类**针对特定代码模式的静态检查规则**。`rustc` 自带一批 lint（如 `unused_variables`），而 **Clippy** 是 Rust 官方维护的一个额外 lint 集合（一个独立工具），专门捕获「能编译、但写得不理想」的模式。它把这些 lint 按主题分成若干**组（group）**，其中与性能直接相关的组就叫 **Perf**。一句话：Clippy 是一个「你写了不太理想的代码，它就提醒你」的助手。

**第二，自动化检测优于手工检测。** perf-book 在本章开头点出一条原则：**自动检测问题，胜过手工检测问题**（automated detection is preferable to manual detection）。正因为 Clippy 默认就能发现一大批性能反模式，本书后续章节**不再重复**那些「Clippy 默认已能检测」的性能问题——否则就是用手工复述机器已经能做的事。这条原则也决定了本讲在整本书里的「**减负**」角色：Clippy 把一大批低级性能坑挡在了门外，让你和后续章节可以专注于它检测不到的问题。

**第三，Clippy 在工作流里的位置。** 前几讲强调「先测量再优化」（u2-l1 基准测试、u2-l2 性能剖析）。Clippy 与它们**互补**而非替代：

| 工具 | 回答的问题 | 成本 | 何时用 |
| --- | --- | --- | --- |
| Clippy（本讲） | 「这段代码有没有**已知的反模式**？」 | 极低，写代码时随手跑 | 全程，写完就跑 |
| Profiler（u2-l2） | 「**热点**在哪里？」 | 中，要配置构建与采样 | 优化阶段 |
| Benchmark（u2-l1） | 「改动后**到底快没快**？」 | 中高，要建基线、控制变量 | 验证每一次改动 |

Clippy 不能告诉你「哪段代码最热」（那是 profiler 的活），也不能量化「改完快了多少」（那是 benchmark 的活），但它能在你**还没想到要测**的时候，就指出「这里有个公认可以改好的写法」。

> 术语提示：本章反复出现 **lint list**，指 Clippy 官方维护的在线 lint 目录：`https://rust-lang.github.io/rust-clippy/master/`。每个 lint 在那里都有一页，说明触发条件与建议改法。

## 3. 本讲源码地图

本讲主源是单一短文件，外加一个为「禁用类型」提供动机的姊妹章节。

| 文件 | 作用 |
| --- | --- |
| [src/linting.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md) | 本书第 4 章正文，按「Basics（Clippy 与 Perf 组、ptr_arg）→ Disallowing Types（disallowed_types）」组织，是本讲唯一主源。 |
| [src/hashing.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md) | 解释了**为什么要禁用** `HashMap`/`HashSet`：默认 `SipHash 1-3` 高质量但偏慢，是 `disallowed_types` 示例选择的动机来源。 |
| [src/SUMMARY.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/SUMMARY.md) | 第 8 行声明 Linting 在目录中的位置，确认它是 Build Configuration 之后、Profiling 之前的正文章节。 |

> 关于「源码即 Markdown」：如 u1-l1 所述，perf-book 是一本 mdBook，其「源码」就是这些 `.md` 文稿。本章里出现的所有 `clippy.toml` 片段与 `cargo clippy` 命令都是**讲解用的示例**，仓库内并没有真实可编译的 Rust 工程或 `clippy.toml`——它们是教读者拿去用的模板。本讲沿用这一约定，凡是要让读者照做的配置都标注「示例配置」。

---

## 4. 核心概念与源码讲解

### 4.1 Clippy 基础与 Perf lint 组

#### 4.1.1 概念说明

Clippy 是一个**lint 集合**，用来「捕获 Rust 代码里的常见错误」（catch common mistakes）。perf-book 一开篇就给它两个定位：

1. 它是**对所有 Rust 代码都值得跑**的优秀工具（excellent tool to run on Rust code in general）；
2. 它**还能帮助提升性能**，因为其中一些 lint 恰好针对「会导致性能不佳」的代码模式。

Clippy 把 lint 按主题分组，与性能直接相关的那个组叫 **Perf**。perf-book 给出一条贯穿全书的写作约定：既然 Clippy 默认就能检出这些性能问题，那么**本书后面的章节就不会再去复述**这些「Clippy 默认已能检测」的问题——避免手工重复机器已胜任的工作。

这一条对本讲读者的直接含义是：**学 Clippy 等于一次性扫掉一大批性能坑**，省得在后面每一章分别记。

#### 4.1.2 核心流程

用 Clippy 检测性能问题的工作流极简：

```text
安装（随 Rust 工具链自带 / rustup component add clippy）
   ↓
cargo clippy        ← 一条命令扫描整个工程
   ↓
对每条告警：阅读建议 → 决定采纳或允许（allow）
   ↓
想看「全部性能 lint 清单」：访问 lint list，只勾选 "Perf" 组
```

- **运行**：装好后在工程根目录执行 `cargo clippy` 即可，用法与 `cargo build` 几乎一致（它本质上是「编译 + 跑 lint」）。
- **查清单**：完整 lint 列表在官方 lint list 页面，默认展示所有组；perf-book 建议**只保留 "Perf" 组、取消勾选其余组**，从而得到「纯性能」的 lint 清单。

#### 4.1.3 源码精读

[src/linting.md:3-6](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L3-L6) —— Clippy 的定位：一组用来捕获常见错误的 lint，对所有 Rust 代码都值得跑，并且其中部分 lint 专门针对「会导致性能不佳」的模式。

[src/linting.md:8-10](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L8-L10) —— 全书的写作约定：自动检测优于手工检测，因此**后续章节不再提及 Clippy 默认已能检测的性能问题**。这是理解「为什么后面的章节不再啰嗦这些坑」的钥匙。

[src/linting.md:16-19](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L16-L19) —— 运行方式极其简单：装好后直接 `cargo clippy`。

[src/linting.md:20-23](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L20-L23) —— 查看全部性能 lint 的方法：访问 lint list，**取消勾选除 "Perf" 外的所有 lint 组**，剩下的就是纯性能 lint 清单。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次 `cargo clippy`，并在官方 lint list 上筛出 Perf 组，建立「性能 lint 长什么样」的直观印象。

**操作步骤**（需要一个任意 Rust 工程，可用 `cargo new demo` 新建）：

1. 确认 Clippy 可用：`cargo clippy --version`（现代 `rustup` 默认自带 clippy 组件；若提示缺失，用 `rustup component add clippy` 补上）。
2. 在工程根目录运行 `cargo clippy`，观察输出：没有问题时会像 `cargo build` 一样报告 `Finished`；有 lint 命中时会打印告警位置与建议。
3. 打开官方 lint list `https://rust-lang.github.io/rust-clippy/master/`，找到 lint 组筛选区，**只保留 Perf 组**，浏览剩下的 lint 名称与简介（例如与不必要的克隆、低效迭代相关的条目）。

**需要观察的现象**：`cargo clippy` 的输出里，每条告警都带 lint 名（形如 `clippy::xxx`）、文件行号，以及一条「建议改成什么」的提示。lint list 上 Perf 组的条目数量适中，每条都点明一种「写得不够快」的典型模式。

**预期结果**：你会看到 Clippy 把「机器能查的事」自动化了——只要照单全收（或逐条判断），就能在不读 profiler 的情况下消灭一大批常见性能反模式。**待本地验证**：具体命中哪些 lint 取决于你的代码。

#### 4.1.5 小练习与答案

**练习 1**：perf-book 说「后续章节不再提及 Clippy 默认能检测的性能问题」，这条声明的底层原则是什么？

> **参考答案**：底层原则是「**自动检测优于手工检测**」。既然 Clippy 已经能自动发现这些反模式，再用人工在每一章里复述就是低效的重复；本书把这部分交给工具，自身只讲 Clippy 检测不到的内容。

**练习 2**：在官方 lint list 上，perf-book 建议你「只保留 Perf 组」。这么做相比「看全部 lint」有什么好处？

> **参考答案**：聚焦性能主题。全量 lint 涵盖正确性、风格、复杂度等多个组，信息量大；只保留 Perf 组能让你**专门审视「与运行速度相关」的 lint**，快速建立「哪些写法会让程序变慢」的清单，不被其它主题分散注意力。

---

### 4.2 ptr_arg：非 Perf lint 也能提升性能

#### 4.2.1 概念说明

4.1 讲了「Perf 组的 lint 能让代码更快」。perf-book 紧接着指出一个**对称的、常被忽略的事实**：**某些本来不属于性能组的 lint，顺带也能提升性能**。

最典型的例子是 `ptr_arg`。它属于**风格（style）类** lint，建议「把各种容器类型的引用参数改成切片」。书里举的例子是把 `&mut Vec<T>` 形参改成 `&mut [T]`。注意它的**首要动机其实是 API 设计**——切片比 `Vec` 更通用（调用方既能传 `Vec`，也能传数组、或另一个切片的子段），而不是性能。但 perf-book 补充了一句关键的话：**它也可能让代码更快**，原因是：

- **更少的间接层（less indirection）**：`&Vec<T>` 是「指向 Vec 的引用」，而 Vec 内部又是「(指针, 长度, 容量)」三元组，访问元素要绕两层；`&[T]` 直接就是「(指针, 长度)」二元组，少一次解引用。
- **更好的优化机会（better optimization opportunities）**：切片形态更贴近编译器与底层内存布局，便于内联、向量化等优化。

这个例子说明一个更广的策略：**好的、符合惯用法的写法，往往同时也是快的写法**。

#### 4.2.2 核心流程

perf-book 用「Conversely（反过来）」一词揭示了一条**双向收益链**：

```text
方向 A：Perf 组 lint
   让代码更快  ──顺带──▶  也让代码更简洁、更符合惯用法
   （因此即便是不常执行的代码也值得采纳）

方向 B：某些非 Perf lint（如 ptr_arg 这类 style lint）
   让代码更简洁、更符合惯用法  ──顺带──▶  也可能让代码更快
   （首要动机是 API 设计，性能是附带收益）
```

两条方向合在一起，结论是：**别只盯着「Perf」标签**。采纳那些让代码更地道的建议，常常**免费**捎带一份性能收益。

#### 4.2.3 源码精读

[src/linting.md:25-27](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L25-L27) —— 方向 A：性能 lint 的建议**通常**也会让代码更简洁、更地道，所以**即便对不常执行的代码也值得采纳**。

[src/linting.md:29-35](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L29-L35) —— 方向 B：反过来，某些**非性能** lint 也能提速。以 `ptr_arg`（一个 style lint）为例，它建议把容器引用参数改成切片，典型如 `&mut Vec<T>` → `&mut [T]`；首要动机是**更灵活的 API**，但**也可能因间接层更少、优化机会更好而更快**。书里还附了一个真实 PR 示例：[fastblur#3](https://github.com/fschutt/fastblur/pull/3/files)。

[src/linting.md:37](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L37) —— `ptr_arg` lint 的官方说明页链接，可查它的精确触发条件（对 `&Vec`/`&mut Vec`/`&String` 等容器引用提出切片化建议）。

#### 4.2.4 代码实践

**实践目标**：在一个含 `&mut Vec<T>` 参数的函数上触发 `ptr_arg`，体验「改成切片」如何同时改善 API 与潜在性能。

**操作步骤**：

1. 新建工程 `cargo new demo`，在 `src/main.rs` 写一个故意「不够地道」的函数（**示例代码**）：

   ```rust
   fn double_all(nums: &mut Vec<i32>) {
       for n in nums.iter_mut() {
           *n *= 2;
       }
   }

   fn main() {
       let mut v = vec![1, 2, 3];
       double_all(&mut v);
       println!("{:?}", v);
   }
   ```

2. 运行 `cargo clippy`，留意是否报 `clippy::ptr_arg`，建议把 `&mut Vec<i32>` 改成 `&mut [i32]`。
3. 按建议改写签名（函数体无需改动），再次 `cargo clippy` 确认告警消失：

   ```rust
   fn double_all(nums: &mut [i32]) { /* 函数体不变 */ }
   ```

4. 体会 API 变化：改完后，调用方除了 `&mut v`，还可以直接传一个数组的可变切片或子段，**调用面更宽**。

**需要观察的现象**：改写前 Clippy 报 `ptr_arg`；改写后告警消失；函数体一行未动；调用方能接受更多种实参。

**预期结果**：你会直观看到「更地道的签名 = 更灵活的 API +（潜在的）更快代码」。**待本地验证**：性能差异在小例子上通常不可测（要在大规模热点数据上才显现），重点是体会 API 与优化机会的双重改善。

#### 4.2.5 小练习与答案

**练习 1**：`ptr_arg` 属于哪个 lint 组？为什么 perf-book 要专门用它举「非 Perf lint 也能提速」的例子？

> **参考答案**：`ptr_arg` 属于 **style（风格）组**，不是 Perf 组。用它举例正好说明：一个**首要目标是 API/风格**的 lint，**顺带**也能减少间接层、改善优化机会而提速——从而证明「性能收益是双向的」，不该只盯着 Perf 标签。

**练习 2**：把 `&mut Vec<T>` 改成 `&mut [T]`，「更少间接层」具体指什么？

> **参考答案**：`&mut Vec<T>` 是「指向 Vec 的引用」，而 Vec 内部是 `(data 指针, len, cap)` 三元组，访问元素要先解引用到 Vec、再取其 `data` 指针，绕了两层。`&mut [T]` 直接是 `(data 指针, len)` 胖指针，访问元素只一次解引用，少一层间接，也更容易被编译器优化。

**练习 3**：既然 `ptr_arg` 的首要动机是「更灵活的 API」，那为什么 perf-book 仍把它放进一本**性能**书里？

> **参考答案**：因为它**附带**性能收益（间接层更少、优化机会更好），而 perf-book 的取向是「广度优先」——凡是对性能有帮助的写法都值得提一句，哪怕它名义上属别的组。这也呼应 4.1 的双向收益链：地道写法常常同时是快写法。

---

### 4.3 disallowed_types：防止误用慢类型

#### 4.3.1 概念说明

perf-book 在后续多章会反复出现一个主题：**有些标准库类型偏慢，换成替代品会更快**。最典型的就是 `HashMap`/`HashSet`——如 `src/hashing.md` 所述，它们的默认哈希算法 `SipHash 1-3` **质量高、抗碰撞能力强，但相对较慢**，尤其对整数这类短键。当你决定全局改用更快的替代（如 `rustc-hash` 提供的 `FxHashMap`/`FxHashSet`），就会冒一个风险：**在某个角落不小心又用回了标准库的 `HashMap`**。这种「漏网之鱼」很难靠肉眼 code review 抓全。

为此 perf-book 介绍了 Clippy 的 `disallowed_types` lint：你可以**把某些类型列为「禁用」**，此后一旦代码里出现它们，Clippy 就会报错提醒。它把「我们团队约定不用某类型」这条**口头约定**，固化成一条**机器强制的规则**。

这个机制与 u2-l3 的「构建配置」异曲同工：都是**不碰业务逻辑、只加配置**就能改变工程行为；区别在于 u2-l3 调编译参数，这里调的是 lint 规则。

#### 4.3.2 核心流程

`disallowed_types` 的启用靠一个 `clippy.toml` 文件：

```text
在工程根目录新建 clippy.toml
   ↓
写入 disallowed-types = ["要禁用的类型全路径", ...]
   ↓
cargo clippy
   ↓
代码里出现被禁类型 → 报 clippy::disallowed_types 告警
```

要点：

- 配置文件名固定为 `clippy.toml`，放在**工程（crate/workspace）根目录**，与 `Cargo.toml` 同级。
- 列表里写类型的**完整路径**，如 `std::collections::HashMap`、`std::collections::HashSet`。
- 一旦配置生效，`cargo clippy` 会把任何对这些类型的使用标记为 `clippy::disallowed_types`。

> 关联 `src/hashing.md`：perf-book 在 [src/hashing.md:47-51](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L47-L51) 明确说——若你决定**统一**改用 `FxHashSet`/`FxHashMap`，就很容易在某些地方**不小心**又用回 `HashSet`/`HashMap`，并指引你「用 Clippy 来避免这个问题」，链接正指向本节的 Disallowing Types。可见这两个章节是配套的：hashing 给出「为什么换」，linting 给出「如何防止换漏」。

#### 4.3.3 源码精读

[src/linting.md:39-44](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L39-L44) —— 动机：后续章节会看到「有时值得避开某些标准库类型、改用更快的替代」；一旦你决定改用替代，就**很容易在某些地方误用回标准库类型**。

[src/linting.md:46-49](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L46-L49) —— 解法：用 Clippy 的 `disallowed_types` lint 规避。例如要禁用标准哈希表（理由见 Hashing 一节），在代码里加一个 `clippy.toml`，内容如 [src/linting.md:50-52](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L50-L52)：

```toml
# 示例配置：禁用标准库哈希表，强制团队改用更快替代（见 hashing.md）
disallowed-types = ["std::collections::HashMap", "std::collections::HashSet"]
```

[src/linting.md:54-55](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/linting.md#L54-L55) —— 两处链接：`[Hashing]` 指向 hashing.md（解释「为什么 HashMap 偏慢」），`disallowed_types` 指向该 lint 的官方说明页。

#### 4.3.4 代码实践

**实践目标**：亲手写一个 `clippy.toml`，禁用 `std::collections::HashMap`，并验证 Clippy 确实会对它的使用报警。

**操作步骤**：

1. 在 `cargo new demo` 的工程根目录（与 `Cargo.toml` 同级）新建 `clippy.toml`（**示例配置**）：

   ```toml
   disallowed-types = ["std::collections::HashMap"]
   ```

2. 在 `src/main.rs` 故意用一次被禁类型（**示例代码**）：

   ```rust
   use std::collections::HashMap;

   fn main() {
       let mut m: HashMap<i32, i32> = HashMap::new();
       m.insert(1, 2);
       println!("{:?}", m);
   }
   ```

3. 运行 `cargo clippy`，观察是否报 `clippy::disallowed_types`，并指出 `HashMap` 不被允许。
4. （延伸）按 `src/hashing.md` 的建议，把依赖换成 `rustc-hash`，改用 `FxHashMap`，并删除 `clippy.toml` 里对 `HashMap` 的禁用（或保留以防止别人再误用），再次 `cargo clippy` 确认告警消失。

**需要观察的现象**：配置前 `cargo clippy` 对 `HashMap` 不报错；配置 `clippy.toml` 后，**同一份代码**立即被标记为 `disallowed_types`；换成未禁用的替代类型后告警消失。

**预期结果**：你会看到 `disallowed_types` 把「团队约定」变成了「机器强制」——配置一处，全工程生效，漏网之鱼无所遁形。**待本地验证**：不同 Clippy 版本的告警文案略有差异，但 lint 名 `disallowed_types` 与触发行为稳定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 perf-book 选 `HashMap`/`HashSet` 作为 `disallowed_types` 的示例？请结合 `src/hashing.md` 说明。

> **参考答案**：因为 `src/hashing.md` 指出标准 `HashMap`/`HashSet` 默认用 `SipHash 1-3`，**高质量但偏慢**（尤其对短键/整数键），并推荐改用 `rustc-hash`（`FxHashMap`/`FxHashSet`）等更快的替代。一旦团队决定全局换用替代，就极易在某些地方误用回标准库版本，所以用 `disallowed_types` 把标准哈希表禁掉，正好堵住这个漏洞。

**练习 2**：`clippy.toml` 应放在哪里？它和 `Cargo.toml` 是同一种东西吗？

> **参考答案**：放在**工程（crate/workspace）根目录**，与 `Cargo.toml` 同级。它**不是** `Cargo.toml`：`Cargo.toml` 是 Cargo 的构建/依赖配置；`clippy.toml` 是 **Clippy 专用**的 lint 配置文件，只影响 Clippy 的行为（如 `disallowed-types`、各类阈值），不参与编译。

**练习 3**：如果团队同时禁用了 `HashMap`，但某段第三方依赖的代码里用到了它，Clippy 会报警吗？这会阻塞构建吗？

> **参考答案**：`disallowed_types` 默认只检查**你自己**的代码，不会对依赖 crate 的内部实现报警（Clippy 默认不 lint 依赖）。即便在你自己的代码里命中，它默认是 **lint 告警（warning）级别**，除非你把它 `deny`（如 `#![deny(clippy::disallowed_types)]`），否则不会让构建失败。是否升级为硬错误由你在代码或配置里决定。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「**用 Clippy 给小工程做一次性能体检 + 定制度量**」的任务。

**任务背景**：你接手一个有若干反模式的小 Rust 工程，目标是：① 用 Clippy 清掉它能自动发现的问题；② 顺手把一个容器引用参数切片化；③ 为后续可能改用快速哈希器铺路——预先用 `disallowed_types` 锁死标准 `HashMap`。

**操作步骤**：

1. **跑 Clippy 体检**（模块 4.1）：`cargo new demo`，写入一段刻意带几个反模式的代码（如 `&mut Vec<T>` 参数、不必要的 `clone` 等），运行 `cargo clippy`，逐条记录命中的 lint 名与建议。
2. **应用 ptr_arg**（模块 4.2）：把其中的 `&mut Vec<T>` 形参按建议改为 `&mut [T]`，确认 `clippy::ptr_arg` 告警消失，并体会 API 变宽（尝试传一个数组切片调用）。
3. **配置 disallowed_types**（模块 4.3）：在工程根目录新建 `clippy.toml`，写入：
   ```toml
   # 示例配置
   disallowed-types = ["std::collections::HashMap", "std::collections::HashSet"]
   ```
   再在 `main.rs` 里写一行 `use std::collections::HashMap;`，运行 `cargo clippy`，确认它被 `clippy::disallowed_types` 命中。
4. **对照 hashing.md 思考**：阅读 [src/hashing.md:8-11](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md#L8-L11)，写下「如果这个工程确有整数键热点哈希，下一步该怎么换成 `FxHashMap`」，并说明 `disallowed_types` 如何在迁移后防止回退。

**预期产物**：一份 Clippy 体检清单 + 一处切片化重构 + 一个生效的 `clippy.toml` + 一段「为何禁用 HashMap」的简短说明。你会切身体会到 Clippy 是「写代码时随手就能跑、且能固化团队约定」的第一道性能防线。

## 6. 本讲小结

- **Clippy 是一组 lint**，用来捕获 Rust 代码的常见错误；其中与性能直接相关的归入 **Perf 组**，可用 `cargo clippy` 一键扫描，并在官方 lint list 上单独筛选 Perf 组查看清单。
- **自动化检测优于手工检测**：perf-book 据此约定，后续章节不再复述 Clippy 默认能检测的性能问题——学 Clippy 等于一次性扫掉一大批性能坑。
- **性能收益是双向的**：Perf 组 lint 让代码「又快又简洁」（故冷代码也值得采纳）；反过来，**某些非 Perf lint（如 style 组的 `ptr_arg`）也能顺带提速**——把 `&mut Vec<T>` 改成 `&mut [T]`，既拓宽 API，又减少间接层、改善优化机会。
- **`disallowed_types` 防止误用慢类型**：在 `clippy.toml` 里写 `disallowed-types = [...]`，可把标准库偏慢的类型（如 `HashMap`/`HashSet`）列为禁用，把「团队约定」固化成机器强制的规则。
- **与 hashing.md 配套**：hashing.md 解释「为什么 `HashMap` 偏慢、该换什么」，本节给出「如何防止换漏」，两者合起来才是一个完整的「替换 + 防回退」闭环。
- **在工作流中的位置**：Clippy 是成本最低、可在编码期就跑的检测，与 profiler（找热点）、benchmark（量化效果）互补，是优化工作流的第一道防线。

## 7. 下一步学习建议

- **横向回到测量**：本讲强调 Clippy 是「写代码时随手跑」的防线。若你还没建立基准与剖析能力，回头补 u2-l1（Benchmarking）与 u2-l2（Profiling）——它们回答 Clippy 回答不了的问题：「哪里最热」「改完到底快没快」。
- **纵向进入代码级优化**：Clippy 清掉反模式后，下一步是理解具体类型与数据结构的性能特性。建议接着读 [src/hashing.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/hashing.md)（本讲 `disallowed_types` 示例的动机来源，u4-l1 会精读）与 [src/heap-allocations.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/heap-allocations.md)（堆分配，第三单元起点）。
- **体会「配置即杠杆」**：本讲的 `clippy.toml` 与 u2-l3 的 `Cargo.toml` profile 同属「不碰业务逻辑、只加配置就能改变行为」的一类手段，可对照阅读，强化「先试无代价的配置/工具改动，再动业务代码」的优化顺序。
- **工具兜底**：想浏览全部 lint（不止 Perf），访问官方 [lint list](https://rust-lang.github.io/rust-clippy/master/)；想在 CI 里把某些 lint 升级为硬错误，可在代码顶部用 `#![deny(clippy::xxx)]` 或在 `clippy.toml`/`Cargo.toml` 里统一配置。
