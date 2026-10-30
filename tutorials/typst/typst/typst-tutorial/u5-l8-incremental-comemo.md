# 增量编译：comemo、track 与 watch 模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 comemo 的追踪模型：`#[comemo::track]` 与 `#[comemo::memoize]` 如何配合，做到「输入没变的部分不重算」，以及它和普通哈希键记忆化的区别。
2. 说清增量解析与增量求值/排版的配合：`Source::edit` 触发的「增量优先、编号失败仅重编号」策略，为什么 span 编号稳定能保住下游缓存。
3. 描述 `typst watch` 的长驻编译循环：依赖监听、`SystemWorld::reset`、`FileStore` 的 Stale Source 复用与 `comemo::evict` 缓存淘汰。
4. 会用 `--timings` 与 watch 的状态栏耗时，对比一次小改动前后两次编译的耗时差异，并按「解析 / 求值 / 布局 + 字体」三个层次解释缓存命中。

## 2. 前置知识

本讲是专家层收尾之一，默认你已读过：

- **u4-l1 编译主流程**：五个阶段（parse → eval → realize → layout → export）、排版多轮迭代与 `constraint.validate` 收敛判定。
- **u2-l1 / u2-l5 语法层**：`Source` 是 `Arc<LazyHash<SourceInner>>` 的不可变快照；`Source::edit` 走增量重解析，span 编号在设计上追求编辑后稳定。
- **u1-l4 CLI**：`compile_once` 是被 `compile` 与 `watch` 复用的单次编译单元。

在此基础上，本讲引入几个新术语：

- **记忆化（memoization）**：把函数调用结果缓存起来，下次「同样的输入」直接返回缓存。关键是「同样」如何判定——comemo 的答案和普通实现不一样。
- **被追踪值（Tracked）**：一个代理句柄。函数通过它读取数据时，comemo 会记账：「这次调用读了什么、读到了什么」。
- **约束（Constraint）**：comemo 的一种精细验证机制，只验证「上次真正读过的那部分输入」在新一轮里是否仍然相同，typst 用它来判定排版迭代是否收敛。
- **长驻编译**：进程不退出，反复编译同一文档的场景（`typst watch`、语言服务器）。此时缓存的生命周期跨越多次编译，必须同时考虑「复用」与「淘汰」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-library/src/lib.rs` | `World` trait 上的 `#[comemo::track]`，以及「谁负责缓存」的责任划分 |
| `crates/typst/src/lib.rs` | `compile` 入口如何 `world.track()`，排版迭代中 `Constraint` 的创建与 `validate` |
| `crates/typst-eval/src/lib.rs` | `eval` 是被 `#[comemo::memoize]` 的求值入口 |
| `crates/typst-layout/src/pages/run.rs`、`crates/typst-layout/src/inline/mod.rs`、`crates/typst-layout/src/inline/shaping.rs` | 排版/断行/整形层的记忆化点 |
| `crates/typst-library/src/introspection/counter.rs` | 以内省器为输入的记忆化示例 |
| `crates/typst-syntax/src/source.rs`、`crates/typst-syntax/src/reparser.rs` | `Source::edit` 与增量重解析（承接 u2-l5） |
| `crates/typst-cli/src/watch.rs`、`crates/typst-cli/src/world.rs`、`crates/typst-cli/src/compile.rs` | watch 主循环、`SystemWorld` 的 reset/dependencies、单次编译计时 |
| `crates/typst-kit/src/files.rs` | `FileStore` 与 `Stale<Source>`：watch 下源文件的「过期但可编辑」状态 |
| `crates/typst-kit/src/watcher.rs`、`crates/typst-kit/src/timer.rs` | 文件监听与计时导出 |
| `crates/typst-timing/src/lib.rs` | `timed!` 宏与 `TimingScope`（RAII 计时事件） |

comemo 本身是外部 crate，版本固定在根 `Cargo.toml` 中：

- [Cargo.toml:L53](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/Cargo.toml#L53) 声明 `comemo = "0.5.1"`，typst 全仓库统一从这里引用。comemo 的内部实现不在本仓库，本讲只基于它在 typst 中的真实用法讲解其行为模型。

## 4. 核心概念与源码讲解

### 4.1 comemo 追踪模型：track 与 memo 如何配合

#### 4.1.1 概念说明

comemo 解决的问题是：**编译器的输入很大（所有源文件、所有字体、整本文档的样式），但单次计算只用其中一小部分**。

普通的记忆化以「整个输入的哈希」为键：输入任何一位变化，键就变，缓存全部失效。对编译器这不可接受——你改了一个字符，不能让 200 个字体和 50 个文件的缓存全作废。

comemo 的做法是把键从「输入的哈希」换成「**函数实际读取过的那部分输入**」：

- 输入侧：`#[comemo::track]` 标注的类型（如 `World`）经由 `.track()` 变成一个被追踪的代理 `Tracked<dyn World>`。函数体通过代理调用 `world.source(id)`、`world.font(i)` 时，comemo 在旁边记账。
- 函数侧：`#[comemo::memoize]` 标注的函数，其结果连同「读取清单」一起缓存。下次调用时，comemo 先用新的输入**重放读取清单**：逐项检查「上次读 `source(A)` 得到 S1，这次读 `source(A)` 是否还是 S1？」全部一致才返回缓存，否则真正重新执行函数体。

行为上等价于：

\[ \text{缓存有效} \iff \forall\, \text{读取} \; r \in \text{读取清单}: \; \text{新输入}(r) = \text{旧输入}(r) \]

这是一个保守判定：只要被读过的部分都没变，结果一定不变；没被读过的部分无论怎么变都不影响。

#### 4.1.2 核心流程

一次 `typst compile` 中，comemo 的参与大致是：

```
compile(world)
  ├─ world.track()                    # 产出 Tracked<dyn World> 代理
  ├─ typst_eval::eval(world, ...)     # #[comemo::memoize]
  │    读取清单: source(main), source(每个 import 的文件), font(k)...
  ├─ 排版循环（最多 MAX_ITERS 轮）
  │    ├─ Constraint::new()
  │    ├─ introspector.track_with(&constraint)   # 带约束地追踪内省器
  │    ├─ T::create(&mut engine, ...)            # 内部到处是 memoize 的布局函数
  │    └─ constraint.validate(新内省器)           # 只验证本轮读过的条目 → 判定收敛
  └─ 返回文档
```

`watch` 场景下进程不退出，第二次 `compile` 传入的还是同一个 comemo 全局缓存：上一次 `eval` 的缓存条目还在，重放读取清单时，「没改动的文件」逐项命中，于是整个 `eval` 对那些文件被跳过（准确说：主文件改了会重新求值，但它 import 的未变文件直接命中缓存的 `Module`）。

#### 4.1.3 源码精读

**① World 是被追踪的输入，缓存责任却留给宿主。**

- [crates/typst-library/src/lib.rs:L47-L62](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L47-L62) 是 `World` trait 的文档注释加 `#[comemo::track]` 标注。注释明确说了责任划分：加载函数（`main/source/file/font`）应由宿主做内部缓存；编译器不自己缓存输入，因为「world 对什么时候会变掌握更多信息」——字体通常不变，可以跨编译缓存（长驻场景如 `typst watch`）；源文件会变，每轮编译后应清空；高级客户端（语言服务器）还可以保留 `Source` 并用 `Source::edit` 原地编辑以获得更好的增量性能。这段注释是理解整讲的设计纲领。
- [crates/typst-library/src/lib.rs:L63-L101](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L63-L101) 是 trait 本体：`library/book/main/source/file/font/today` 七个方法就是 comemo 会记账的「读取集」。

**② 编译入口把 world 换成代理。**

- [crates/typst/src/lib.rs:L74-L83](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L74-L83) `compile` 第一件事就是 `world.track()`（第 80 行），把 `&dyn World` 变成 `Tracked<dyn World>` 再传给内部实现——从此编译器对 world 的一切访问都在记账。

**③ 求值是记忆化的。**

- [crates/typst-eval/src/lib.rs:L38-L47](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-eval/src/lib.rs#L38-L47) `eval` 同时挂着 `#[comemo::memoize]` 和 `#[typst_macros::time]`。它的参数里既有 `Tracked<dyn World>`（追踪输入），也有普通引用 `&Source`（直接参与哈希键）。`Source` 外层是 `LazyHash`，哈希算一次即缓存（见 4.2.3），所以把它当普通键成本很低。

**④ 布局、断行、整形各层都有记忆化点。**

- [crates/typst-layout/src/pages/run.rs:L75-L88](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/run.rs#L75-L88) `layout_page_run_impl` 带 `#[comemo::memoize]`；公开的 `layout_page_run`（[L56-L73](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/run.rs#L56-L73)）只是把 `Engine` 拆包成追踪参数再转调。这是 typst 里「外壳函数拆 Engine、内层函数 memoize」的标准样板。
- [crates/typst-layout/src/inline/mod.rs:L66-L79](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/mod.rs#L66-L79) 段落布局 `layout_par_impl` 同样如此。
- [crates/typst-layout/src/inline/shaping.rs:L1221-L1222](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/inline/shaping.rs#L1221-L1222) 整形计划（shape plan）也是记忆化的——同一文字系统反复整形时直接复用。

**⑤ 内省器作为输入的记忆化，靠 Constraint 精细验证。**

- [crates/typst-library/src/introspection/counter.rs:L918-L929](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/introspection/counter.rs#L918-L929) 计数器序列计算 `sequence_impl` 的参数里有 `Tracked<dyn Introspector>`。内省器每轮排版都会整体更新，若按整体哈希比对，缓存永远失效；comemo 的约束机制让它只比对「实际查过的那几个 location」。
- [crates/typst/src/lib.rs:L139-L159](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst/src/lib.rs#L139-L159) 是排版迭代主循环：第 145 行 `comemo::Constraint::new()` 创建约束，第 151 行 `introspector.track_with(&constraint)` 把约束挂到内省器代理上，第 157 行 `T::create` 执行本轮排版，第 159 行 `constraint.validate(document.introspector())` 用新一轮的内省器验证「本轮读过的条目是否都没变」。没变 → 收敛，跳出循环。这正是 u4-l1 讲过的收敛快路径在 comemo 层的落点。

全仓库共有 59 处 `#[comemo::memoize]`，分布在 37 个文件（统计方法见 4.1.4），覆盖求值、排版、整形、PDF/SVG 导出、图像解码、插件调用等各层——记忆化不是某一个大开关，而是撒在流水线各处的小缓存点。

#### 4.1.4 代码实践

**实践目标**：把「记忆化点分布在哪里」变成一张自己整理的地图，直观感受增量缓存是分层的。

**操作步骤**：

1. 在仓库根目录执行（`rg` 即 ripgrep，typst 仓库开发环境一般已具备；没有可用 `grep -rn` 替代）：

   ```bash
   rg -n '#\[comemo::memoize\]' crates/ --stats
   ```

2. 按目录分组统计：`typst-eval`、`typst-layout`、`typst-library`、`typst-pdf`/`typst-svg`/`typst-render`、`typst-bundle`/`typst-html` 各有多少处。
3. 挑三处看上下文（各往上读 10 行左右）：`crates/typst-eval/src/lib.rs` 的 `eval`、`crates/typst-layout/src/pages/run.rs` 的 `layout_page_run_impl`、`crates/typst-library/src/foundations/plugin.rs` 中的任意一处，确认它们都遵循「外壳拆 Engine、内层 memoize」或「纯函数 + 追踪输入」的模式。

**需要观察的现象**：统计输出中每个匹配文件名后跟计数；三个样板函数的参数列表里都能找到 `Tracked<...>` 形态的参数。

**预期结果**：本讲撰写时（HEAD 84cca406a）统计为 **59 处、37 个文件**，其中 `typst-library`（图像、字体、插件、计数器等）与 `typst-layout`（页面、段落、整形、collect）占比最大。若你统计时 HEAD 已前进，数字可能小幅变化，这是正常的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `eval` 的缓存不会因为 `today()` 返回值变化而意外失效或意外命中？

**答案**：`today` 是 `World` 的方法，属于被追踪的读取集。若某文档求值时调用了 `sys.datetime`，读取清单里会记下 `today(offset)`；下一次编译 comemo 重放这一项，日期没变就命中、变了就重新求值。没读过 `today` 的文档的缓存则完全不受日期影响——这正是「按读取集而非整体输入做键」的好处。

**练习 2**：`layout_page_run_impl` 的参数中 `children: &[Pair]` 不是 `Tracked`，它是怎么参与缓存判定的？

**答案**：非追踪参数直接参与缓存键的哈希比对。`Pair`（元素 + 样式链）本身可哈希（样式链、`Content` 都是为此设计的可哈希结构），字节级一致即命中。追踪参数（world、introspector）走「重放读取清单」的宽松判定，两者结合：内容没变 + 读过的环境没变 = 直接复用。

**练习 3**：如果没有 comemo 的 Constraint 机制，只有「整体哈希内省器」这一种判定，u4-l1 的排版迭代会发生什么？

**答案**：每轮迭代内省器整体都不同（页码、location 在变），所有以它为键的布局缓存每轮全失效，五轮迭代等于五次全量排版，且无法用「读取集未变」来判定收敛，只能整体比对两轮文档。有了 Constraint，`constraint.validate` 只检查本轮实际查询过的条目，未受影响的页可以整页复用缓存。

### 4.2 增量解析配合：Source::edit、reparse 与 span 稳定性

#### 4.2.1 概念说明

comemo 记忆化解决「输入没变就不重算」，增量解析解决「**让输入尽可能看起来没变**」。

传导链是这样的：

```
编辑一段文本
  → 增量重解析：只重建受影响子树（快）
  → span 编号稳定：未变节点的编号不变
  → 求值产物 Content 的哈希不变（Content 内嵌 span）
  → 布局/整形缓存命中
```

反过来，如果每次编辑都全树重解析、全部重新编号，那么即使文字内容 99% 未变，所有 Content 的哈希也会因 span 变化而全部不同——comemo 再聪明也无缓存可命中。所以 u2-l5 讲过的「编号只占区间前半留空隙」「替换时修剪公共前后缀」等设计，本质上都是在为这一讲的缓存命中率服务。

#### 4.2.2 核心流程

入口 `reparse` 的三层策略（承接 u2-l5，这里从缓存视角复述）：

```
reparse(root, text, replaced, replacement_len):
  1. root.reparse_inner_node_mut() 下钻
     → try_reparse_and_renumber(...)
        a. 单个子节点完全包住编辑区间 → 递归下钻或整块重解析（块节点）
        b. markup 层 → 指数扩展兄弟范围，reparse_markup 局部重解析
        c. 成功后 renumber_children 局部重编号（编号不够时指数扩边）
  2. 下钻成功但编号失败 → 不重解析！只在根节点重新 numberize
  3. 下钻失败 → parse(text) 全量重解析
```

注意第 2 层是「增量优先」的精髓：**重解析成功但编号失败时，宁可全树重新编号，也不放弃已经拿到的局部替换结果**——因为保住局部替换就保住了子树对象 identity 与大部分 span，缓存价值仍在。

#### 4.2.3 源码精读

**① 编辑入口。**

- [crates/typst-syntax/src/source.rs:L90-L104](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L90-L104) `Source::edit`：先 `Arc::make_mut` 写时复制（别的克隆不受影响），更新行表，然后第 103 行调用 `reparse`。返回值是「新文本中真正被重解析的区间」——这个区间越小，编辑越便宜。
- [crates/typst-syntax/src/source.rs:L69-L88](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L69-L88) `Source::replace`：对新旧文本做朴素的前后缀 diff，找出最小编辑再调 `edit`。watch 场景下「文件被编辑器重写」就靠它把一次全文件覆盖降解为一次小编辑。
- [crates/typst-syntax/src/source.rs:L34-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L34-L41) `Source::new` 把整棵树连同文本包进 `Arc<LazyHash<...>>`——哈希一次计算终身复用，这就是 4.1.3 里 `&Source` 能直接当 memoize 键的原因。

**② 增量优先、失败仅重编号。**

- [crates/typst-syntax/src/reparser.rs:L16-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L16-L41) `reparse` 入口：第 24 行 `renumber` 标志初始为 true；第 26-31 行尝试下钻增量重解析，成功则记录实际重解析区间，且仅当编号结果出错（第 31 行 `num_result.is_err()`）才保留「需要重编号」的状态；第 36-38 行在根节点补一次 `numberize`。只有下钻整体失败才走第 33 行的全量 `parse(text)`。
- [crates/typst-syntax/src/reparser.rs:L43-L71](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L43-L71) 的文档注释写明了搜索策略与边界（只在顶层或 markup 块内重解析 markup 表达式、不重解析数学、不跨警告节点），并与旧实现的历史取舍做了说明。

**③ span 稳定性是被显式追求的性质。**

- [crates/typst-syntax/src/reparser.rs:L370-L375](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L370-L375) `replace_children` 的文档注释直说：「为了提高 span 编号的稳定性，前后与旧节点相同的部分不做替换」。
- [crates/typst-syntax/src/reparser.rs:L376-L420](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L376-L420) 实现：用 `spanless_eq` 修剪公共前缀（L383-L391）与公共后缀（L393-L401），真正 splice 的只是中间差异段（L416）。被修剪掉的旧节点对象原样保留——编号自然不变。
- [crates/typst-syntax/src/reparser.rs:L422-L474](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L422-L474) `renumber_children`：在「前一个兄弟的上界」与「后一个兄弟的编号」之间找空号，放不下就按 2 的幂向两侧扩展重编范围（L471-L472），尽量少动邻居。

**④ 与下游缓存的衔接。**

把 ① ② ③ 连起来：watch 下用户改了一个词 → `Source::replace` 降解为一次小 `edit` → `reparse` 只替换 markup 里的少数兄弟节点、修剪公共前后缀 → 未变子树 span 原封不动 → 该文件因 `LazyHash` 哈希变化而重新 `eval`（memoize 键变了，这是必要代价），但求值产出的 Content 中，来自未变子树的部分哈希与上一轮完全一致 → `layout_par_impl`、整形计划等记忆化点大量命中。**增量解析不直接省求值的钱，它省的是排版的 钱，手段是保住 Content 的哈希。**

#### 4.2.4 代码实践

**实践目标**：亲眼看到「一次小编辑只重解析小区间，且未变兄弟节点的 span 不变」。

**操作步骤**（源码阅读 + 临时测试，做完请还原，不要提交）：

1. 先读现成测试，理解判定框架：[crates/typst-syntax/src/reparser.rs:L547-L572](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L547-L572) 的 `test` 辅助函数。它断言两件事：增量树与全量 `parse` 的树在 `spanless_eq` 意义下完全一致（L558）；返回的重解析区间符合预期（L566-L571）。
2. 在本地分支给 `reparser.rs` 的 `mod tests` 临时加一个测试（**示例代码**，展示思路）：

   ```rust
   #[test]
   fn test_span_stability_observation() {
       let id = FileId::new_for_test("span_stability");
       let text = "= Title\n\nFirst paragraph.\n\nSecond paragraph.";
       let mut source = Source::new(id, text);

       // 编辑前：记录根节点各孩子的 span
       let before: Vec<_> = source.root().children().map(|c| c.span()).collect();

       // 把 "First" 改成 "Third"
       let at = text.find("First").unwrap();
       source.edit(at..at + 5, "Third");

       let after: Vec<_> = source.root().children().map(|c| c.span()).collect();

       // 观察点 1：打印实际重解析区间（应由 edit 返回值给出，远小于全文）
       // 观察点 2：比较 before/after 中未受影响孩子的 span 是否逐一相等
       println!("{:?}", before);
       println!("{:?}", after);
   }
   ```

3. 运行 `cargo test -p typst-syntax span_stability -- --nocapture` 观察输出。

**需要观察的现象**：`edit` 返回的重解析区间只覆盖被改的词附近；前后两份孩子 span 列表中，未受影响的段落节点编号完全相同。

**预期结果**：未变兄弟 span 逐项相等；若某个孩子在重解析范围内被重编，仅它及其后需要重编的节点变化。具体打印值**待本地验证**（取决于 `children()` 切分粒度与编号分配），这也是本实践想让你建立的直觉。

#### 4.2.5 小练习与答案

**练习 1**：既然 `Source` 哈希变了会导致整个文件重新 `eval`，为什么还说增量求值有效益？

**答案**：`eval` 是按文件记忆化的：主文件变了只重求值主文件，它 `#import` 的未变文件在 `eval` 的读取清单里逐项命中，直接返回缓存的 `Module`，不会重新执行那些文件里的脚本。多文件项目里这是主要收益。

**练习 2**：`reparse` 在「替换成功、编号失败」时为什么选择在根节点全树重编号，而不是回退到全量重解析？

**答案**：全量重解析会丢弃局部替换的成果，重建所有节点对象并重新分配所有编号，未变子树的 span 大概率漂移，下游 Content 哈希全部变化、缓存大面积失效。仅重编号虽然也会改动编号（当区间编号耗尽时），但它发生在 `replace_children` 修剪之后，能不动的兄弟仍然不动，通常只有局部编号变化，缓存保住得多。

**练习 3**：`Source::replace` 的「前后缀 diff」在什么情况下会退化？退化后增量解析还有意义吗？

**答案**：在文件开头和结尾同时变化、或整个文件被重排时，公共前后缀很短，diff 出的编辑区间接近全文，增量近似全量。仍有意义：只要编辑区间能被局部重解析覆盖且前后孩子可修剪，编号稳定性带来的缓存收益仍在；真正最坏情况是区间大到下钻失败，那就回退 `parse(text)` 全量重解析，正确性不受影响。

### 4.3 watch 长驻编译：事件循环、世界重置与缓存生命周期

#### 4.3.1 概念说明

`typst watch` 把「单次编译」变成「常驻服务」。长驻带来两个新问题：

1. **正确性**：怎么知道该重新编译、重编译时要丢弃哪些状态？——文件监听 + 世界重置。
2. **内存与性能的平衡**：缓存跨编译保留才有增量收益，但永不清理会无限膨胀；而「标记文件为可能已变」又不能把旧 `Source` 直接扔掉，否则丢失增量编辑的机会。

typst 的答案分三层：watcher 只监听**上一轮真正读过的文件**（依赖集）；`World::reset` 把文件槽标记为「过期但保留旧值」（`Stale<Source>`）；comemo 全局缓存继续存在，用 `evict` 淘汰久未使用的条目。

#### 4.3.2 核心流程

`watch` 的主循环（对照源码）：

```
初始化:
  Timer（--timings 时启用）→ CompileConfig::watching → Watcher（忽略输出文件自身）
  → 循环创建 SystemWorld（输入/root 不存在则等文件出现）
  → 预先扫描字体（不计入编译耗时）→ compile_once（第一次编译）

loop {
    watcher.update(world.dependencies())   // 只监听上一轮读过的文件，标记清扫
    watcher.wait()                         // 文件事件批量合并后唤醒
    world.reset()                          // 文件槽 → Empty(Stale<Source>)，时间重置
    timer.record(compile_once(...))        // 复用 comemo 缓存 + Stale Source 原地编辑
    comemo::evict(10)                      // 淘汰一段时间未用的缓存条目
}
```

#### 4.3.3 源码精读

**① watch 主循环。**

- [crates/typst-cli/src/watch.rs:L19-L85](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/watch.rs#L19-L85) `watch` 全函数。几个值得停留的点：
  - L28：`Watcher::new` 传入输出文件路径——写 PDF 触发的事件要被忽略，否则自己写输出会引发无休止重编译。
  - L32-L50：`SystemWorld::new` 失败且是输入/根目录不存在时，把这个路径加入监听并 `wait`，等文件出现再试——watch 一个还不存在的文件也能工作。
  - L56-L58：分页输出时预先 `scan_fonts`，注释说明字体扫描耗时高度依赖系统，提前做掉是为了不污染显示出的编译时间。
  - L61：初始编译。
  - L69-L84：主循环。L71 `watcher.update(world.dependencies())` 用上一轮的依赖集更新监听；L74 `watcher.wait()`；L77 `world.reset()`；L80 重新编译；L83 `comemo::evict(10)` 淘汰缓存条目（参数为时长阈值，精确语义由 comemo 定义），防止长驻进程内存无限增长。
- [crates/typst-cli/src/compile.rs:L287-L314](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/compile.rs#L287-L314) `compile_once` 第 292 行 `Instant::now()` 计时，watch 模式下把耗时包进 `Status::Success(duration)` / `PartialSuccess` / `Error` 打印——这就是实践里要读的「第二次编译耗时」的来源。状态消息的格式化在 [crates/typst-cli/src/watch.rs:L148-L163](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/watch.rs#L148-L163)。

**② 世界的重置与依赖收集。**

- [crates/typst-cli/src/world.rs:L107-L116](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L107-L116) `SystemWorld::dependencies` 返回上一轮编译实际访问过的文件路径（这就是 watcher 的监听集）；`reset` 只做两件事：`files.reset()` 与 `now.reset()`（当前时间每轮重取，除非命令行固定了时间戳）。注意 `library` 与 `fonts` 字段（[L25-L38](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/world.rs#L25-L38)）不在 reset 之列——标准库和字体跨编译保留，正对应 4.1.3 里 World 文档注释的缓存责任划分。

**③ FileStore：过期但可编辑的源文件。**

- [crates/typst-kit/src/files.rs:L101-L116](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/files.rs#L101-L116) `FileStore::reset` 的文档注释说得非常直白：重置后所有已加载文件标记为过期，后续访问会重新加载；但与「整店新建」不同，**源文件会用新数据原地编辑（edit in place），从而获得更好的增量编译性能**。
- [crates/typst-kit/src/files.rs:L128-L159](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/files.rs#L128-L159) `FileSlot` 枚举与 `Stale<T>` 类型别名：每个槽位除了当前状态还携带一份「过期的 `Source`」。
- [crates/typst-kit/src/files.rs:L167-L174](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/files.rs#L167-L174) `FileSlot::reset`：把 `Parsed(Ok(source), _)` 里的 source 取出存进 `Empty(Some(source))`——旧树没扔，只是标记过期。
- [crates/typst-kit/src/files.rs:L190-L237](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/files.rs#L190-L237) `FileSlot::source`：下一轮编译再要这个文件时，L213-L219 发现有过期 source，就从磁盘读新字节后调 `source.replace(new)`——4.2 里讲的 `Source::replace → edit → reparse` 增量链正是从这里被触发的。这一段是「watch 的增量性能」的真正接头点。

**④ Watcher：依赖集的标记清扫。**

- [crates/typst-kit/src/watcher.rs:L18-L33](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/watcher.rs#L18-L33) `Watcher` 结构：底层是 `notify` 的 `RecommendedWatcher`，`watched` 表带布尔「本轮是否见过」标记，`missing` 集合存尚不存在、只能轮询的路径。
- [crates/typst-kit/src/watcher.rs:L38-L45](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/watcher.rs#L38-L45) 三个时间常数：事件批量合并窗口 100ms、批量上限 500ms、缺失文件轮询间隔 300ms——编辑器保存文件时常触发一串事件，合并避免连环重编译。
- [crates/typst-kit/src/watcher.rs:L76-L118](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/watcher.rs#L76-L118) `update` 是标准的标记-清扫：先把所有已监听路径标记为未见过，遍历新依赖集补监听、标记见过，最后把没见过的取消监听。于是监听集永远精确等于「上一轮编译读过的文件」。

**⑤ 计时：--timings 与 timed!。**

- [crates/typst-cli/src/args.rs:L465-L471](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-cli/src/args.rs#L465-L471) `--timings` 参数：产出 Chrome tracing 格式的 JSON，可加载进 <https://ui.perfetto.dev> 这类工具。
- [crates/typst-kit/src/timer.rs:L46-L95](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-kit/src/timer.rs#L46-L95) `Timer::new_or_placeholder` 与 `record`：路径含 `{n}` 时按 watch 迭代序号编号（L68-L80），所以 watch 下每次重编译各得一份 trace 文件；每份记录前先 `typst_timing::clear()`。
- [crates/typst-timing/src/lib.rs:L34-L44](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-timing/src/lib.rs#L34-L44) `timed!` 宏与 [L152-L205](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-timing/src/lib.rs#L152-L205) `TimingScope`：RAII 计时，构造时记开始事件、`Drop` 时记结束事件，全局开关由 `enable()` 控制（[L67-L72](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-timing/src/lib.rs#L67-L72)），事件序列化成 Chrome tracing JSON 在 [L101-L150](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-timing/src/lib.rs#L101-L150)。在 perfetto 里你能直接看到第二次编译中哪些 `timed!` 作用域（eval、page run、layout par…）变短或消失了。

#### 4.3.4 代码实践

**实践目标**：量化 watch 下一次小改动的编译加速，并按层次归因。

**操作步骤**：

1. 准备一个有多页内容的 `doc.typ`（例如几十段文字加一个 `#import "chapter.typ"` 的子文件），子文件里也放几段内容。
2. 启动（任选其一，仓库构建版或安装版）：

   ```bash
   cargo run -p typst-cli -- watch doc.typ --timings trace{n}.json
   # 或已安装的 CLI：typst watch doc.typ --timings trace{n}.json
   ```

3. 记录初始编译耗时（状态栏 `compiled successfully in ...`），此时得到 `trace0.json`。
4. 在 `chapter.typ` 中改动一个词并保存，等状态栏刷新，记录第二次编译耗时，得到 `trace1.json`。
5. 把 `trace0.json` 与 `trace1.json` 都拖进 <https://ui.perfetto.dev>，对比同名作用域（如 `eval`、`page run`、`layout par`）的时长与数量。
6. 退出 watch（Ctrl+C），再做一个对照实验：不用 watch，连续两次运行 `typst compile doc.typ`——两次都是冷进程，第二次不会更快，体会「增量是缓存带来的，而缓存活在进程里」。

**需要观察的现象**：第二次编译耗时明显小于第一次；`trace1.json` 中主文件与子文件的 `eval` 作用域仍会出现（进程内 `FileStore` 重置后需要重新加载），但排版相关作用域总时长大幅下降。

**预期结果**：小改动下第二次编译通常显著快于首次（数量级取决于文档规模与机器，具体数值**待本地验证**）。归因层次：

| 层次 | 是否命中 | 机制 |
| --- | --- | --- |
| 解析 | 部分 | `Stale<Source>` → `Source::replace` → 增量 `reparse`，只重解析小区间 |
| 求值 | 部分 | memoized `eval`：改动的文件重跑，未变的 import 文件命中缓存的 `Module` |
| 布局/整形 | 大量命中 | 未变子树 span 稳定 → Content 哈希不变 → `layout_page_run_impl`、`layout_par_impl`、整形计划复用 |
| 字体 | 完全命中 | `SystemWorld` 的字体扫描与缓存不参与 `reset`，跨编译保留 |

#### 4.3.5 小练习与答案

**练习 1**：为什么 watcher 必须忽略输出文件自身的写事件？如果 `--timings` 的 trace 文件恰好写在被监听目录里，会发生什么？

**答案**：watch 自己写输出（PDF/PNG）会触发文件事件，不忽略就会「编译→写输出→触发事件→再编译」死循环。trace 文件一般不在依赖集里（依赖集只含上一轮 `world.source/file` 读过的文件，见 `FileStore::dependencies`），所以写 trace 不会被监听；但若某文档恰好 `#read` 了它，就会被纳入依赖集引发重编译——这是理解「监听集 = 依赖集」的最好例子。

**练习 2**：`world.reset()` 为什么不直接清空 `FileStore`、重建所有槽位？

**答案**：重建会丢掉旧 `Source`，下一轮只能 `Source::new` 从零解析、从零编号，span 全部漂移，下游布局缓存全灭。保留 `Stale<Source>` 后走 `source.replace(new)` 的增量编辑路径，才是 watch 增量性能的来源（files.rs 的文档注释明说这一点）。

**练习 3**：`comemo::evict(10)` 若被删掉，watch 长时间运行会怎样？

**答案**：comemo 全局缓存只增不减，随时间积累所有出现过的输入组合的缓存条目（每次编辑都产生新的 `Source` 哈希与新条目），内存持续增长。evict 按「多久未再使用」淘汰条目，把增量收益与内存占用折中。具体淘汰规则由 comemo crate 定义（外部依赖，版本见根 Cargo.toml）。

## 5. 综合实践

设计一个能同时体现三层机制的对照实验：

1. **搭建文档**：`main.typ` 通过 `#import` 引入 `a.typ` 与 `b.typ` 两个子文件，每个子文件两三页内容，主文件再加一个跨页的长列表（制造大量可缓存的布局单元）。
2. **基线测量**：`typst watch main.typ --timings trace{n}.json`，记录首次编译耗时与 `trace0.json`。
3. **局部编辑**：只改 `a.typ` 末尾一个词，保存，记录耗时与 `trace1.json`；再只改 `main.typ` 里列表中的一项，记录耗时与 `trace2.json`。
4. **结构编辑**：在 `main.typ` 顶部插入一行 `#set page(margin: 3cm)`，记录耗时与 `trace3.json`。
5. **分析**：
   - 对比 trace1/trace2/trace3 中 `eval`、`page run`、`layout par` 等作用域的总时长与出现次数；
   - 解释为什么第 3 步（改样式）几乎使排版缓存全灭（页边距进入 `Styles` → 参与 `layout_page_run_impl` 的键），而第 1、2 步大部分布局可复用；
   - 用一句话把结论接回源码：局部文字编辑 → [`Source::replace`](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L69-L88) → [`reparse` 增量优先](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L16-L41) → [span 稳定](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/reparser.rs#L370-L375) → [memoize 命中](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-layout/src/pages/run.rs#L75-L88)；样式级编辑 → 键变化 → 缓存失效。

具体耗时数值**待本地验证**，重要的是三种编辑之间的相对差异及其在 trace 中的体现。

## 6. 本讲小结

- comemo 的追踪模型把记忆化键从「整体输入哈希」换成「**实际读取集**」：`#[comemo::track]` 的 `World` 经 `world.track()` 变成记账代理，`#[comemo::memoize]` 的函数缓存「结果 + 读取清单」，下次调用重放清单逐项验证。全仓库 59 处 memoize 点分布在求值、排版、整形、导出各层。
- `Constraint` 是读取集验证在内省迭代上的应用：`track_with(&constraint)` 记录本轮读过哪些内省条目，`constraint.validate` 只验证这些条目——既支撑跨轮布局缓存，也是 u4-l1 收敛判定的快路径。
- 增量解析「增量优先、替换成功但编号失败只在根重编号、下钻失败才全量重解析」；`replace_children` 修剪公共前后缀保住未变兄弟的 span，从而使未变子树的 `Content` 哈希不变、下游布局/整形缓存命中。增量解析不省求值的钱，省的是排版的 钱。
- `typst watch` 的循环是「按依赖集监听 → wait → `world.reset()` → `compile_once` → `comemo::evict`」；`FileStore::reset` 把源文件槽位置为 `Empty(Stale<Source>)`，下一轮访问时用 `Source::replace` 原地编辑，这是增量解析接入 watch 的接头点；字体与标准库不参与 reset，跨编译保留。
- `--timings` 借 `timed!`/`TimingScope`（RAII）导出带 `{n}` 迭代编号的 Chrome tracing JSON，是观察「第二次编译少了哪些工作」的直接工具。

## 7. 下一步学习建议

- **u5-l9 插件系统**：`crates/typst-library/src/foundations/plugin.rs` 里有 3 处 `#[comemo::memoize]`——WASM 调用同样被记忆化，看看外部计算如何纳入增量体系。
- **重读 u5-l6 typst-ide**：语言服务器是「长驻编译」的另一个宿主，`crates/typst-ide/src/utils.rs` 中 `world.upcast().track()` 与本讲的 `world.track()` 同源，对照观察 IDE 如何保留 `Source` 并编辑以获得增量性能。
- **comemo crate 本体**（<https://github.com/typst/comemo>，本仓库固定 0.5.1）：想深究「读取清单重放」的内部实现（线程局部缓存表、约束的位向量表示）可读其源码，注意版本与本仓库锁定的版本对齐。
- **回看 u4-l7 内省机制**：本讲的 `constraint.validate` 与 u4-l7 的五轮迭代上限是一体两面——缓存判定负责快路径，`History` 重放负责诊断警告。
