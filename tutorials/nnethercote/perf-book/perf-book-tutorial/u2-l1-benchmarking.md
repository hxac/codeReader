# Benchmarking——建立可比较的性能基线

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **benchmarking（基准测试）到底在干什么**：它不是“测一次有多快”，而是“把两个做同一件事的程序放在一起比较”，尤其是“同一程序改动前后两个版本”的比较；
- 理解 **为什么优化前必须先建立可重复的基准**——没有基线，你就无法回答“这次改动到底有没有变快”；
- 能根据 perf-book 给出的线索，区分 **真实工作负载、微基准（microbenchmark）、压力测试（stress test）** 的取舍；
- 认识 perf-book 推荐的 **常用基准测试工具**（Rust 内建 benchmark tests、Criterion、Divan、Hyperfine、Bencher、Gungraun、自定义框架如 rustc-perf）及其各自适用场景；
- 掌握 **wall-time、cycles、instruction counts** 三类指标的“方差高低”权衡，明白为什么“贴近用户感受”的指标往往噪声也最大；
- 亲手用 `cargo build --release` 构建两个略有差异的版本，并用 Hyperfine 对比，体会作者那句 **“平庸的基准测试也胜过没有基准测试”**。

本讲是第二单元（性能优化工作流：先测量再优化）的第一篇。u1 三个单元你已经能把 perf-book“读对、跑起来、找得到章节”；从本讲起，我们正式进入“怎么动手优化一个 Rust 程序的性能”。而优化的第 0 步不是改代码，而是 **先会量**——本讲就是教你“量”的。

## 2. 前置知识

在继续前，请确认你已经理解（u1-l1、u1-l2、u1-l3 已建立）：

- **perf-book 是一本用 mdBook 渲染的在线技术书**，它的“源码”就是 `src/` 下的 Markdown 文件，**不是可运行的 Rust 库**。所以本讲引用的“源码”是 `src/benchmarking.md` 这一章的**文字内容**，而不是某个函数实现。
- perf-book 用 **mdBook** 渲染，`src/SUMMARY.md` 是目录的单一事实来源；`benchmarking.md` 在书里是 **第 2 章**（紧跟 Introduction）。
- 本书面向 **中高级 Rust 用户**，写作上 **重广度轻深度**，深度靠外链补足——本讲会多次让你点开外部工具链接去补深度。

几个本讲要用到、但可能对你陌生的术语，先打个预防针：

- **基准测试（benchmarking）**：反复运行一段程序并测量其性能，用来做“前后对比”或“方案 A vs 方案 B”的判断。
- **工作负载（workload）**：你用来“压”程序的那份具体输入与操作场景。基准测试的结果是否有意义，首先取决于工作负载选得对不对。
- **微基准（microbenchmark）**：只测一个极小函数、脱离真实使用场景的小测试，优点是聚焦、缺点是容易“测了等于没测”。
- **方差（variance）**：多次测量结果之间的波动大小。方差大 = 每次测得不一样 = 结果不可信。
- **wall-time（墙上时间）**：程序从开始到结束真实流逝的时间，也就是用户拿秒表能测到的那个时间。

> 一句话定位：本讲不读 Rust 实现源码，而是把 `src/benchmarking.md` 当作“性能测量方法论”来精读，并配一个真正能跑起来的 Rust + Hyperfine 小实验。

## 3. 本讲源码地图

本讲只围绕 **一个核心源文件** 展开（这正是 perf-book“一章一事”风格的好处）：

| 文件 | 角色 | 本讲用来理解什么 |
| --- | --- | --- |
| `src/benchmarking.md` | 全书第 2 章，讲基准测试方法论 | 基准测试的定义、工作负载选择、工具清单、指标与方差权衡 |

辅助理解（**关联章节**，本讲只点到为止，后续单元会深入）：

- `src/profiling.md`（第 5 章，u2-l2）：基准测试告诉你“变快了吗”，剖析（profiling）告诉你“热点在哪”。二者配合才是完整的测量闭环。
- `src/build-configuration.md`（第 3 章，u2-l3）：本讲综合实践会用到 `cargo build --release`；release 背后的 `codegen-units`、`LTO`、`opt-level` 等选项在第 3 章系统讲解。
- `src/general-tips.md`（第 18 章，u6-l1）：本章反复强调“只优化热点”“算法优先”，与基准测试“先量再改”的思路一脉相承。

## 4. 核心概念与源码精读

本讲拆成三个最小模块：

1. **4.1 基准测试的作用与工作负载** —— 为什么先量、拿什么来量。
2. **4.2 常用基准测试工具** —— 用什么工具去量（工具决定了你能拿到什么指标）。
3. **4.3 指标选择：wall-time、cycles、instruction counts** —— 量出来的数字该怎么读、为什么噪声这么大。

---

### 4.1 基准测试的作用与工作负载

#### 4.1.1 概念说明

先纠正一个常见误解：**基准测试不是“测出我的程序每秒跑多少次”，而是“把两个做同一件事的程序放在一起比较”**。perf-book 开篇就把它定义得很清楚——基准测试的本质是“比较”。

比较的对象分两种：

- **不同的程序做同一件事**：例如 Firefox vs Safari vs Chrome，比谁渲染同一组网页更快；
- **同一程序的不同版本**：例如你改了一行代码，比“改动前”和“改动后”谁更快。

对做性能优化的工程师来说，**第二种才是日常最关心的**，因为它能可靠地回答那个核心问题：**“我这次的改动，到底让程序变快了吗？”** 没有这种“前后对比”，你改代码就像蒙眼调参——你以为快了，其实可能是噪声。

这就是为什么 perf-book 把 Benchmarking 排在“优化工作流”的第一章：**它是后续一切代码优化的前提**。不建立基线，后面 u3 的堆分配优化、u4 的迭代器优化、u5 的内联优化，全都无法判断“到底有没有用”。

有了“为什么要量”，接下来是“拿什么来量”——也就是**工作负载（workload）**。理想情况下，你应该有一组**多样化的、能代表程序真实用法**的工作负载。真实世界的输入最好；微基准（microbenchmark）和压力测试（stress test）可以适度使用，但不能喧宾夺主。

#### 4.1.2 核心流程

基准测试驱动优化的整体闭环可以这样描述：

```
① 选工作负载（workloads）
     真实输入（最佳）> 微基准 / 压力测试（适度使用）
        │
② 选测量工具 ──── 这一步同时决定了你能拿到哪些「指标」
        │
③ 测「改动前」版本  →  得到基线（baseline）
        │
④ 改代码
        │
⑤ 测「改动后」版本  →  得到新数据
        │
⑥ 对比基线与新数据，回答：「这次改动更快了吗？」
        │
     ├─ 是 → 保留改动，更新基线
     └─ 否 → 回退改动，重新想办法
```

关键结论：**步骤 ③ 的“基线”是整条链的锚点**。基线不可重复（每次测都不一样），后面所有判断都站不住脚。所以本单元 u2 的核心命题就是“先测量再优化”——**没有可信的基线，就没有可信的优化**。

#### 4.1.3 源码精读

先看 perf-book 对 benchmarking 的一锤定音式定义：

[src/benchmarking.md:1-7](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L1-L7) —— 这 7 行讲清了 benchmarking 的本质：它“通常涉及比较两个或多个做同一件事的程序的性能”。注意第 5–7 行特别点出“比较同一程序的两个不同版本”这种情形，因为它能让我们**可靠地回答“这次改动有没有让它变快”**。这一句就是整个优化工作流的理论基石。

作者紧接着给自己划了边界——本节只讲基础：

[src/benchmarking.md:9-10](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L9-L10) —— 坦承“基准测试是个复杂话题，全面覆盖超出本书范围，这里只讲基础”。这印证了 u1-l1 提到的本书“重广度轻深度”的风格：它给你地图，深度靠外链和你自己补。

然后是工作负载的选择原则：

[src/benchmarking.md:12-15](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L12-L15) —— “首先你需要工作负载来测量。理想情况下，应有一组多样化的、能代表程序真实用法的工作负载。使用真实世界输入的工作负载最好，但微基准和压力测试可以适度使用。”这句话定下了优先级：**真实输入 > 微基准/压力测试**。它没有否定微基准的价值（“适度使用”），但提醒你别本末倒置——一个脱离真实场景、跑得飞快的微基准，可能根本不代表程序在生产里的表现。

这两个概念（微基准、压力测试）在原文里是带外链脚注的：

[src/benchmarking.md:17-18](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L17-L18) —— 第 17 行是“微基准”的 Stack Overflow 解释链接，第 18 行是“压力测试”的维基百科链接。这正是本书“深度靠外链”的典型写法：新术语第一次出现就给权威出处，不展开。

#### 4.1.4 代码实践

**实践目标**：把“工作负载优先级”从抽象原则变成你能动手填写的清单。

**操作步骤**：

1. 重新读一遍 [src/benchmarking.md:12-15](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L12-L15)。
2. 选一个你熟悉的程序（你手头的某个 Rust 项目、或一个假想的“JSON 解析器”/“HTTP 服务”都行）。
3. 为它列出 **3 个候选工作负载**，并按下表分类（真实输入 / 微基准 / 压力测试），再判断每一个“是否代表真实用法”：

   | 候选工作负载 | 分类（真实/微基准/压力） | 是否代表真实用法？ | 若用它会忽略什么？ |
   | --- | --- | --- | --- |
   | 例：用线上抓的 1000 个真实 JSON | 真实输入 | 是 | —— |
   | 例：循环解析同一个 10 字节 JSON 一百万次 | 微基准 | 否 | 忽略了大文件、深嵌套、异常输入 |
   | …（请你填） | | | |

**需要观察的现象**：当你逐个填“若用它会忽略什么”这一列时，会发现**单一工作负载必然有盲区**——这正是 perf-book 强调“一组多样化的工作负载”的原因。

**预期结果**：你得到一张能直观看出“为什么不能只靠一个微基准下结论”的对照表。本步骤纯阅读与思考，无需运行任何命令，因此结果可立即得出。

#### 4.1.5 小练习与答案

**练习 1**：有人写了一个微基准，测出“函数 A 比函数 B 快 5 倍”，于是把项目里所有地方都换成 A，结果整体性能几乎没变。用本节的观点，最可能的原因是什么？

> **答案**：微基准脱离了真实使用场景。函数 A 可能在“被独立反复调用”时快，但在真实程序里它可能很少被调用、或调用时的输入分布完全不同，所以单独的 5 倍在整体里被稀释。这正是 perf-book 说微基准只能“适度使用”、且“真实输入最好”的原因。

**练习 2**：为什么 perf-book 把“比较同一程序的两个版本”看得比“比较两个不同程序”更重要（对优化工程师而言）？

> **答案**：因为优化的本质是“改动”，而判断改动是否有效，唯一可靠的方式就是比较“改动前”和“改动后”这两个版本。比较两个不同的程序（如 Firefox vs Chrome）回答的是“谁更强”，而比较两个版本回答的是“我的这次改动有没有用”——后者才是日常优化迭代需要的高频反馈。

---

### 4.2 常用基准测试工具

#### 4.2.1 概念说明

选好工作负载后，你需要“一种运行工作负载的方式”。perf-book 在这里埋了一个关键洞见：**你选的运行方式，同时也就决定了你能拿到哪种指标**。换句话说，“工具”和“指标”不是先后两步，而是同一步的两面——你用 Hyperfine 跑命令行，拿到的就是 wall-time；你用 Gungraun 做 `cargo bench` 集成，拿到的就是高精度指令级测量。

perf-book 给出的工具清单（按原文顺序）：

| 工具 | 性质 | 适用场景 | 关键限制/特点 |
| --- | --- | --- | --- |
| **Rust 内建 benchmark tests** | 语言自带（`#[bench]`） | 最简单的起点 | 依赖 unstable 特性，**只能用于 nightly Rust** |
| **Criterion** | 第三方 crate（`cargo bench`） | 函数级、进程内基准测试 | 比 `#[bench]` 更成熟，是社区主流之一 |
| **Divan** | 第三方 crate（`cargo bench`） | 函数级、进程内基准测试 | Criterion 的现代替代选项之一 |
| **Hyperfine** | 独立命令行工具 | **整程序**级基准测试 | 通用、易上手，本讲综合实践就用它 |
| **Bencher** | 平台/服务 | **持续**基准测试（含 GitHub CI） | 适合把基准纳入 CI，长期跟踪回归 |
| **Gungraun** | crate（`cargo bench` 集成） | 高精度测量 | 前身叫 *Iai-callgrind*（见下方提示） |
| **自定义框架** | 自建 | 特殊需求 | 例如 rustc-perf，专门给 Rust 编译器做基准 |

> 关于 Gungraun 的名字：当前 HEAD（`a05dd0f`）的最近一次提交正是 *“Iai-callgrind has been renamed as Gungraun.”*。所以你在网上或老资料里看到的 *Iai-callgrind*，对应的就是这里的 Gungraun。perf-book 已经把名字更新过来了。

一个总览式判断：**Criterion / Divan / Gungraun 适合“测一个函数或一个 crate 内部的热路径”；Hyperfine 适合“测一个完整的可执行程序”；Bencher 适合“把基准长期挂在 CI 上防回归”**。当然这只是粗略分工，具体取舍要看你的程序形态。

#### 4.2.2 核心流程

“按程序形态选工具”的决策树：

```
你要测的对象是什么？
│
├─ 一个函数 / 一段热路径（进程内）
│     └─ Criterion / Divan / Gungraun（走 cargo bench）
│           └─ 它们能精细控制迭代、统计，部分能拿到指令级指标
│
├─ 一个完整的可执行程序（命令行）
│     └─ Hyperfine（外部计时）
│           └─ 简单通用，但只能拿到 wall-time 类指标
│
├─ 想在 CI 里长期跟踪、防止性能悄悄回归
│     └─ Bencher（持续基准测试，支持 GitHub CI）
│
└─ 程序太特殊（如编译器本身）
      └─ 自定义框架（如 rustc-perf）
```

核心结论：**“你需要一种运行工作负载的方式，而这种方式也决定了所用的指标。”** 这句话是本节的纲领——选工具不是单纯挑顺手的，而是先想清楚“我想看哪种指标”，再倒推该用哪类工具。指标的话题在 4.3 展开。

#### 4.2.3 源码精读

perf-book 用一个引导句 + 项目符号清单给出全部工具：

[src/benchmarking.md:20-21](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L20-L21) —— “其次，你需要一种运行工作负载的方式，而这同时也决定了所用的指标。”注意“**dictate the metrics**”这个措辞：工具决定指标。这是理解后面 4.3 的钥匙。

接下来是逐个工具的一句话点评（注意每条的侧重点都不同）：

[src/benchmarking.md:22-23](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L22-L23) —— Rust 内建 benchmark tests：“是个简单的起点，但它们用的是 unstable 特性，因此只在 nightly Rust 上可用。”**关键限制：必须 nightly**。如果你在 stable Rust 上 `cargo bench` 用 `#[bench]` 会报错，这就是为什么多数人转而用 Criterion。

[src/benchmarking.md:24-24](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L24-L24) —— Criterion 和 Divan：“是更成熟的替代方案。”一句话把两个 crate 并列为 `#[bench]` 的升级选项。

[src/benchmarking.md:25-25](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L25-L25) —— Hyperfine：“是一款优秀的通用基准测试工具。”关键词 **general-purpose（通用）**——它不绑定 Rust，任何命令行程序都能测，这也是本讲综合实践选它的原因。

[src/benchmarking.md:26-26](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L26-L26) —— Bencher：“能在 CI 上做持续基准测试，包括 GitHub CI。”关键词 **continuous（持续）**——它解决的是“长期跟踪、防回归”，而非一次性测量。

[src/benchmarking.md:27-28](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L27-L28) —— Gungraun：“提供与 `cargo bench` 集成的高精度测量。”关键词 **high-precision（高精度）**——它走的是指令级（callgrind 风格）测量，方差极低，正好呼应 4.3 讲的“低方差指标”。

[src/benchmarking.md:29-30](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L29-L30) —— 自定义框架：“也是可行的。例如 rustc-perf 就是用来给 Rust 编译器做基准的框架。”说明当通用工具不够时，特殊程序值得自建框架。

这些工具都附了外链（脚注式链接定义）：

[src/benchmarking.md:32-38](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L32-L38) —— 这里集中放了 benchmark tests（Rust 官方 unstable 文档）、Criterion、Divan、Hyperfine、Bencher、Gungraun、rustc-perf 的链接。本书“深度靠外链”的风格在这里又一次体现：要真正上手某个工具，点对应链接去读它的文档。

#### 4.2.4 代码实践

**实践目标**：把“工具 → 适用场景”的映射从记忆变成判断力，并为本讲综合实践选好工具。

**操作步骤**：

1. 重新读一遍 [src/benchmarking.md:20-30](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L20-L30) 的工具清单。
2. 针对下面 4 个场景，各选出**最合适的一个工具**，并说明理由：
   - 场景 A：你想知道一个纯函数 `fn hash(s: &[u8]) -> u64` 两种实现的耗时差异。
   - 场景 B：你有一个编译好的命令行程序 `myapp`，想比较 release 构建前后它的整体启动+运行时间。
   - 场景 C：你想在每个 PR 上自动跑基准，防止有人悄悄引入性能回退。
   - 场景 D：你在 stable Rust 上工作，不想装 nightly，但又想用 `cargo bench`。
3. 打开 [src/benchmarking.md:32-38](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L32-L38) 里的对应外链，确认你选的工具确实覆盖该场景。

**需要观察的现象**：你会发现 **B 场景（整程序）几乎只能用 Hyperfine**，而 A/C/D 场景则各有侧重（A→Criterion/Divan/Gungraun，C→Bencher，D→Criterion/Divan，因为 `#[bench]` 要 nightly）。

**预期结果**：你能为每个场景给出有依据的工具选择，并理解“工具决定指标”这条纲领——例如选 Hyperfine 就意味着你主要拿 wall-time。本步骤为阅读与判断，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 perf-book 说 Rust 内建 benchmark tests 只能当“起点”？

> **答案**：因为它依赖 unstable 特性，**只能在 nightly Rust 上用**，而多数项目跑在 stable 上。所以它适合“快速试一下”，但不适合长期、跨工具链稳定的基准。社区因此发展出 Criterion、Divan 这些能在 stable 上用的成熟替代。

**练习 2**：Hyperfine 和 Criterion 都能做基准测试，它们最本质的区别是什么？

> **答案**：**粒度与边界不同**。Criterion（以及 Divan、Gungraun）是**进程内**的，你把被测函数注册进去，它精细控制迭代次数、做统计，适合测一个函数/热路径；Hyperfine 是**外部命令行**工具，它把你的程序当成一个黑盒可执行文件来计时，适合测**整程序**的端到端时间。也因此 Hyperfine 拿到的基本是 wall-time，而进程内工具往往能配合拿到 cycles、指令数等更细的指标。

**练习 3**：Bencher 和前面几个工具的根本不同在哪？

> **答案**：Bencher 面向的是 **continuous benchmarking（持续基准测试）**，核心价值是把基准**纳入 CI（含 GitHub CI）长期跟踪**、发现性能回归，而不是做一次性的单次测量。前面几个工具回答“现在有多快 / 这次改动快了吗”，Bencher 还回答“过去三个月这个基准有没有悄悄变慢”。

---

### 4.3 指标选择：wall-time、cycles、instruction counts

#### 4.3.1 概念说明

有了工具、跑出了数字，接下来要回答：**这个数字可信吗？** 这就牵出本节的核心张力——**越贴近用户感受的指标，往往方差（噪声）也越大**。

perf-book 把指标问题讲得很坦率：没有“唯一正确”的指标，**合不合适取决于被测程序的性质**。比如批处理程序看重的指标，对交互式程序可能毫无意义。

最常见的三类指标：

- **wall-time（墙上时间）**：程序真实流逝的时间。它最直观——**对应用户实际感知到的快慢**，所以是“许多情况下的明显选择”。但它**方差高**。
- **cycles（CPU 周期数）**：硬件层面的周期计数，比方墙上时间稳定一些。
- **instruction counts（指令数）**：程序执行的指令条数，**方差最低**，常被当作低噪声的替代指标。

为什么 wall-time 方差高？perf-book 给了一个非常关键、且贯穿全书后续多个章节的解释：**内存布局（memory layout）上的微小变化，就能引发显著但短暂的性能波动**。这句话听起来抽象，但它正是 u3（堆分配/类型大小）、u4（数据结构）等章节要解决的根本问题——程序里数据在内存里的排布哪怕只挪了一个字节，都可能让 CPU 缓存命中情况大变，于是同一段代码这次跑 100ms、下次跑 120ms，纯粹是“运气”。这种噪声让你很难判断“2% 的提升”到底是真改进还是抖动。

于是有了**用低方差指标（cycles、instruction counts）做替代**的思路：同样的输入，程序执行的指令数几乎不变，不受内存布局抖动影响，所以更适合做精细的“A vs B”比较。代价是：指令数**不反映缓存、内存、分支预测**这些真实影响速度的因素——它“稳”但“不完全等于快”。

最后还有个容易被忽略的难点：**当你有多个工作负载时，怎么把多组测量汇总成一个结论？** perf-book 说这本身就是一个挑战，方法很多，没有哪种明显最优。所以基准测试不只是“跑数字”，还包括“怎么读数字”。

#### 4.3.2 核心流程

三类指标在“贴近真实 / 方差高低”上的权衡，可以画成一张取舍图：

```
                  贴近用户真实感受
                  ▲
                  │  wall-time（墙上时间）
                  │    ✓ 直接等于用户感知的快慢
                  │    ✗ 方差最高（受内存布局抖动影响）
                  │
                  │  cycles（周期数）
                  │    ~ 较贴近硬件真实
                  │    ~ 方差中等
                  │
                  │  instruction counts（指令数）
                  │    ✗ 不反映缓存/内存/分支
                  │    ✓ 方差最低，适合精细 A/B 对比
                  ▼
                  方差低、结果稳定可重复
```

量化“方差高不高”，基准工具常用**变异系数**：

\[
CV = \frac{\sigma}{\mu}
\]

其中 \(\sigma\) 是多次测量的标准差，\(\mu\) 是均值。\(CV\) 越大，说明每次测得越不一致、结果越不可信。Hyperfine 等工具会在输出里直接报告这类离散度指标。**wall-time 的 \(CV\) 通常明显大于 instruction counts 的 \(CV\)**——这就是为什么精细对比时人们更爱看指令数。

可以这么记这条权衡律：

\[
\text{贴近真实} \;\updownarrow\; \text{方差高}
\]

越想贴近真实速度，就得忍受越大噪声；越想要稳定可重复，就越要离开“纯真实时间”、借助 cycles / 指令数这种更“机械”的指标。**没有免费午餐**——这就是为什么 perf-book 说“没有哪个指标明显最优”。

#### 4.3.3 源码精读

指标讨论集中在原文这一段：

[src/benchmarking.md:40-47](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L40-L47) —— 这 8 行是本节的精华。要点拆解：
- “指标有很多选择，正确的那个取决于被测程序的性质”——**没有普适最优指标**；
- “批处理程序合适的指标，对交互式程序可能没意义”——**指标与程序形态绑定**；
- “wall-time 在许多情况下是明显选择，因为它对应**用户所感知到的东西**”——这是 wall-time 的核心优点；
- “但它可能方差很高”——核心缺点；
- “尤其是，**内存布局上的微小变化会引发显著但短暂的性能波动**”——方差高的根因，也是全书后续优化章节的伏笔；
- “因此，方差更低的其它指标（如 cycles 或 instruction counts）可能是合理的替代”——**低方差替代方案**。

特别把“内存布局抖动”这一句单独拎出来，因为它太重要：

[src/benchmarking.md:45-47](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L45-L47) —— “它（wall-time）可能方差很高。尤其是，内存布局上的微小变化会引发显著但短暂的性能波动。因此，方差更低的其它指标（如 cycles 或 instruction counts）可能是合理的替代。”**请记住这句**——等你学到 u3 的 `type-sizes`（用 `-Zprint-type-sizes` 重排字段）、u3 的堆分配、u4 的数据结构布局时，会不断回到这个根因：**性能抖动常常来自数据在内存里换了位置，导致缓存行为变化**。

多个工作负载如何汇总，是一个单独的难题：

[src/benchmarking.md:49-50](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L49-L50) —— “汇总多个工作负载的测量结果也是个挑战，方法多种多样，没有哪种明显最优。”提醒你：跑完数字还要决定“怎么把它们综合成一个结论”，这一步同样没有标准答案。

最后是 perf-book 给基准测试新手的“心理按摩”，也是本讲反复呼应的金句：

[src/benchmarking.md:52-57](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L52-L57) —— “好的基准测试很难。话虽如此，**别太纠结于拥有完美的基准测试设置，尤其是刚开始优化一个程序时。平庸的基准测试远胜于没有基准测试**。对你正在测量的东西保持开放心态，随着你对程序性能特征的了解加深，可以逐步改进基准测试。”这段话定调了本讲的实践态度：**先用一个粗糙但能跑的基准开始，迭代改进，不要因为追求完美而迟迟不开始测量。**

#### 4.3.4 代码实践

**实践目标**：亲手感受“wall-time 方差高”这件事，建立对噪声的直观认识（为综合实践做铺垫）。

**操作步骤**（任何能反复运行的小命令都行，下面以 Linux 为例）：

1. 准备一个能稳定重复运行、又有点耗时的命令。例如解析本仓库的一个较大 Markdown 文件、或对一个列表排序。这里用 `sleep` 模拟一个耗时操作作为**示例命令**（非 perf-book 内容）：
   ```bash
   for i in 1 2 3 4 5 6 7 8 9 10; do
     /usr/bin/time -f "%e" sleep 0.1 2>&1
   done
   ```
   （`%e` 是墙上时间，单位秒；`sleep 0.1` 让单次约 0.1 秒。）
2. 看输出的 10 个数字，找出最大值与最小值。
3. 用 4.3.2 的公式算这组数的 \(CV=\sigma/\mu\)（手算或用 `awk`）。

**需要观察的现象**：

- 即使是 `sleep 0.1` 这种“理应精确”的操作，10 次的墙上时间也不会完全一样，会有几毫秒到十几毫秒的抖动；
- 哪怕只是 `sleep`，\(CV\) 也几乎不可能正好是 0。

**预期结果**：你直观看到“墙上时间天然有噪声”。**具体数值待本地验证**（取决于机器负载、调度器、计时精度），但“多次测量结果有波动”这一现象是确定的。这正是 perf-book 警告 wall-time“方差高”的现实体现。

> 说明：如果你装了 Hyperfine，本步骤可以直接用 `hyperfine --runs 10 'sleep 0.1'`，它会自动算出均值与离散度，更省事——这正好通向本讲的综合实践。

#### 4.3.5 小练习与答案

**练习 1**：你改了一个函数，基准显示 wall-time 提升了 2%。你能直接下结论“这个改动让程序变快了”吗？为什么？

> **答案**：不能轻易下结论。因为 wall-time 方差高，2% 的差异**完全可能在噪声范围之内**（recall 4.3.3 说的“内存布局微小变化会引发显著但短暂的波动”）。更稳妥的做法是：多次重复测量看 \(CV\)、或改用 cycles / instruction counts 这种低方差指标复核，确认这 2% 不是抖动。

**练习 2**：instruction counts 方差极低、结果稳定，为什么 perf-book 仍然没有把它说成“最优指标”？

> **答案**：因为指令数**不反映缓存命中、内存访问、分支预测**这些真正决定运行速度的因素。一段代码指令数少，不代表跑得快（可能缓存miss更严重）。它只是“低噪声的替代指标”，适合精细 A/B 对比，但**不等于真实速度**。所以 perf-book 说“没有哪个指标明显最优”，指标选择取决于程序性质。

**练习 3**：perf-book 说“平庸的基准测试远胜于没有基准测试”。这句话对刚开始优化的人，实际指导意义是什么？

> **答案**：它鼓励你**尽早开始测量、而不是等到搭出“完美”基准才动手**。一个粗糙但能跑、能前后对比的基准，已经足以帮你判断“改动方向对不对”；随着你对程序性能特征了解加深，再逐步把基准改进得更可信（换更低方差的指标、加更多真实工作负载等）。**先有基线，再求精**——这是整个 u2 工作流的态度。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个“建基线 → 改一版 → 对比”的完整闭环，亲身体验作者那句 **“平庸的基准测试也胜于没有基准测试”**。

**任务**：用 `cargo build --release` 构建一个 Rust 程序的**两个略有差异的版本**，再用 **Hyperfine** 对比它们的 wall-time，并记录对比结果。

### 操作步骤

1. **创建一个最小 cargo 项目**（任意空目录即可；以下命令均为示例命令，非 perf-book 内容）：
   ```bash
   cargo new bench_demo
   cd bench_demo
   ```
2. **写两个做“同一件事”但实现不同的二进制**。在 `src/bin/` 下放两个文件，这样一次 `cargo build --release` 会同时产出两个可执行文件：
   - `src/bin/sum_loop.rs`（版本 A：朴素循环求和）
     ```rust
     use std::env;
     fn main() {
         // 从命令行读 N，避免编译期常量折叠
         let n: u64 = env::args().nth(1).unwrap().parse().unwrap();
         let mut s: u64 = 0;
         for i in 1..=n {
             s += i;
         }
         println!("{s}"); // 打印结果，阻止编译器把整段计算删掉
     }
     ```
   - `src/bin/sum_formula.rs`（版本 B：等差数列 closed-form）
     ```rust
     use std::env;
     fn main() {
         let n: u64 = env::args().nth(1).unwrap().parse().unwrap();
         let s: u64 = n * (n + 1) / 2;
         println!("{s}");
     }
     ```
   版本 B 用了数学公式：
   \[
   \sum_{i=1}^{n} i = \frac{n(n+1)}{2}
   \]
   这是 perf-book 在 `general-tips.md`（u6-l1）强调的“算法/数据结构优先于底层技巧”的一个缩影——**换个算法，常常比任何底层优化都来得彻底**。
3. **构建两个 release 版本**：
   ```bash
   cargo build --release
   ```
   产物在 `target/release/sum_loop` 和 `target/release/sum_formula`。
4. **安装 Hyperfine**（它不在 perf-book 仓库里，需自行安装）：
   ```bash
   cargo install hyperfine
   ```
5. **用 Hyperfine 对比**（`--warmup 3` 先空跑 3 次热身，再正式计时）：
   ```bash
   hyperfine --warmup 3 \
     './target/release/sum_loop 1000000000' \
     './target/release/sum_formula 1000000000'
   ```
   （`1000000000` = 10⁹；此时和约 5×10¹⁷，远小于 `u64` 上限 ~1.8×10¹⁹，不会溢出。）

### 需要观察的现象

- Hyperfine 会分别报告两个命令的**平均 wall-time**和**离散度**（如标准差或 \(CV\)），并给出“B 比 A 快多少倍”的对比；
- 预期 `sum_formula`（版本 B）**显著快于** `sum_loop`（版本 A），因为 B 是 O(1)，A 是 O(n)；
- 注意观察 Hyperfine 报告的离散度——你会看到即使是同一个程序多次运行，wall-time 也有波动（呼应 4.3 的“方差高”）。

### 预期结果

- 你应当看到 **版本 B 远快于版本 A**；
- 同时看到 **wall-time 带有噪声**，从而理解为什么精细对比需要关注离散度、或改用低方差指标。

**关于具体数字**：具体的毫秒数与倍数**待本地验证**（取决于你的 CPU、负载、Hyperfine 版本），**本讲不预设任何运行结果**。但“公式版快于循环版”这一结论在原理上是确定的。

> ⚠️ 一个很可能出现、且非常有教育意义的“意外”：**如果两个版本都快到接近 0ms、看不出差异**，多半是 **LLVM 把循环版 `sum_loop` 也识别成了求和模式、直接优化成了公式**（编译器的归纳变量优化）。这本身就是基准测试里一个经典陷阱——**编译器可能把你正想测的工作直接删掉或改写**。如果遇到这种情况，记录下来，然后把 `sum_loop` 的循环体换成编译器无法折叠的形式，例如一个有数据依赖的混合运算 `s = s.wrapping_mul(31).wrapping_add(i);`（注意这改变了“做的事”，仅用于演示“逼编译器保留工作”），重新对比，就能看到真实差异。这个“差点被编译器骗了”的经历，正是 perf-book 让你“对测量的东西保持开放心态”的最好注脚。

> 还原：综合实践是在你自己的 `bench_demo` 目录里进行的，**不影响 perf-book 仓库**，无需 `git checkout`；做完后直接删除该目录即可。

## 6. 本讲小结

- **基准测试的本质是“比较”**：尤其是“同一程序改动前后两个版本”的比较，它能可靠回答“这次改动有没有变快”——这是整个优化工作流的前提（[src/benchmarking.md:1-7](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L1-L7)）。
- **工作负载优先级**：真实世界输入 > 微基准 / 压力测试（适度使用）；单一工作负载必有盲区，应有一组多样化的工作负载（[src/benchmarking.md:12-15](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L12-L15)）。
- **工具决定指标**：你选的运行方式同时决定了能拿到哪种指标。Rust 内建 benchmark tests（nightly 限定）、Criterion/Divan（进程内函数级）、Hyperfine（整程序、通用）、Bencher（CI 持续）、Gungraun（高精度，原 Iai-callgrind）、自定义框架（如 rustc-perf）各有定位（[src/benchmarking.md:20-30](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L20-L30)）。
- **wall-time 贴近真实但方差高**：内存布局的微小变化会引发显著但短暂的波动；cycles / instruction counts 方差更低，是不反映缓存/内存的“机械”替代指标（[src/benchmarking.md:40-47](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L40-L47)）。
- **没有普适最优指标，汇总多工作负载也没有唯一最佳方法**（[src/benchmarking.md:40-50](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L40-L50)）。
- **平庸的基准测试远胜于没有基准测试**：先有粗糙基线再求精，不要因追求完美而迟迟不开始测量（[src/benchmarking.md:52-57](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/benchmarking.md#L52-L57)）。

## 7. 下一步学习建议

本讲让你学会了“建基线、做对比”——但基准测试只能告诉你**“变快了吗”**，不能告诉你**“慢在哪里、热点是谁”**。要回答后者，需要进入 **剖析（profiling）**：

- **u2-l2 Profiling**：学习用 perf / samply / flamegraph / DHAT 等剖析器定位热点函数，并为 release 构建开启调试信息与帧指针。对应原文 [src/profiling.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/profiling.md)（第 5 章）。**基准测试 + 剖析 = 完整的“先测量”闭环**。
- **u2-l3 Build Configuration**：本讲综合实践用了 `cargo build --release`，但 release 背后的 `codegen-units`、`LTO`、`opt-level`、`panic`、`target-cpu` 等选项如何影响运行速度、二进制体积与编译时间，在第 3 章系统讲解。对应 [src/build-configuration.md](https://github.com/nnethercote/perf-book/blob/a05dd0f15595e98aef45e5a15072c2a71dbe37ba/src/build-configuration.md)。
- 如果你想先把“测量三件套”（基准 → 剖析 → 构建配置）补齐再开始改代码，建议按 **u2-l2 → u2-l3** 的顺序学完本单元，再进入 u3 的代码级优化。

> 一句话衔接：本讲给你“秤”（基准测试），下一讲给你“放大镜”（剖析）——先用秤称出有没有变快，再用放大镜找出为什么慢，两者配合，后面的代码优化才有方向。
