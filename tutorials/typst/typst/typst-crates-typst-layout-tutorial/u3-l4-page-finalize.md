# 页面最终化 finalize：组装完整页面

## 1. 本讲目标

本讲是「页面与文档布局」单元（u3）的第四篇，承接 u3-l3 的 `layout_page_run`。前一讲我们看到，`layout_page_run` 能把两次分页符之间的连续正文排成「半成品」`LayoutedPage`——它已经排好了正文（`inner`）、页眉、页脚、前景、背景，却故意留下一个缺口：**左右页边距还没有按奇偶页互换**。

学完本讲，你应该能够：

- 说清楚为什么 `finalize` 必须发生在并行排版**之后**，并且必须串行执行；
- 解释 `binding.swap(counter.physical())` 如何根据物理页号决定是否互换左右边距与左右出血；
- 复述页面拼装的固定顺序 `background → header → inner → footer → foreground`，并说明这个顺序为什么会影响 `counter`（计数器）和内省（introspection）的结果；
- 区分「逻辑页号」与「物理页号」，说出两者何时一致、何时不一致。

## 2. 前置知识

阅读本讲前，请确认你已经了解以下概念（它们都在前面几讲建立过）：

- **`Frame` 是排版结果的唯一载体**：一张页面最终就是一个 `Frame`，内部可以嵌套子 `Frame`（通过 `FrameItem::Group`）。参见 u2-l3。
- **`Frame` 的压入顺序就是内省顺序**：内省器（introspector）按 `Frame` 中元素的出现顺序来解析 `query`、`counter`、`label`。所以「先 push 什么、后 push 什么」不是无关紧要的细节。参见 u2-l4、u3-l5。
- **`layout_pages` 的三段式**：`collect`（切分）→ `parallelize`（并行排版 page run）→ `finalize`（串行组装）。`finalize` 是最后一段。参见 u1-l4、u3-l2、u3-l3。
- **并行排版的前提是各 page run 互相独立**：u3-l3 解释过，`LayoutedPage` 把「值 + `two_sided` 标记」一起暂存，正是为了把依赖物理页号的工作推迟到串行的 `finalize`。

一个通俗类比：`layout_page_run` 像是在各个车间**并行**地把每页的零件造好（正文、页眉、页脚……），但「这页是左页还是右页」「这页的页码是几」要等所有零件造完、按顺序排到流水线上之后才知道。`finalize` 就是流水线末端那个**串行**的总装工位：拿到零件、确认页号、贴上边距、拧上页码，产出最终的 `Page`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/pages/finalize.rs` | **主角**。唯一的函数 `finalize`，把 `LayoutedPage` 总装成最终 `Page`。 |
| `src/pages/run.rs` | 定义半成品 `LayoutedPage` 的结构，以及 `binding`、`number_align`、各 marginal 尺寸的来源（u3-l3 已讲，本讲会回溯引用）。 |
| `src/pages/mod.rs` | `layout_pages` 在串行循环里调用 `finalize`，并维护跨页的 `tags` 与 `counter`。 |
| `src/document.rs` | 定义最终产物 `Page` 结构（本讲末尾会看清它的字段）。 |
| `crates/typst-library/.../introspection/counter.rs` | 定义 `ManualPageCounter`——逻辑页号 / 物理页号的双轨计数器。 |
| `crates/typst-library/.../layout/page.rs` | 定义 `Binding` 枚举及其 `swap` 方法。 |

> 说明：`ManualPageCounter` 与 `Binding` 实际定义在兄弟 crate `typst-library`，`typst-layout` 只负责消费。这符合 u1-l2 讲过的分工——类型骨架在 library，排版逻辑在 layout。

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：

- **4.1 `pages/finalize`**：`finalize` 的总体流程，以及它为何必须串行。
- **4.2 `Binding`**：左右页边距互换的判定逻辑。
- **4.3 `ManualPageCounter`**：逻辑页号与物理页号的双轨管理。

### 4.1 `pages/finalize`：把半成品总装成最终页面

#### 4.1.1 概念说明

`finalize` 是 `layout_pages` 三段式的**最后一段**，职责是：接收一个已经排好版但「缺物理页号」的 `LayoutedPage`，结合当前已知的物理页号，把它组装成最终的 `Page`。

为什么要有这一步、而且必须串行？根本原因是**双面打印的边距互换**（two-sided margins）。当文档设置成双面（`two_sided`）时，左右边距要按「内边距 / 外边距」（inside / outside）解释：奇数页和偶数页的左右边距是镜像的。而一页是奇数还是偶数，取决于它在整篇文档里的**物理页号**——这个页号在并行排版阶段是不知道的（因为并行任务各自独立，谁也不知道自己最终排在第几页）。

`finalize.rs` 顶部的注释把这件事说得很直白：

[finalize.rs:9-11](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L9-L11) —— 注释说明：inside/outside 边距需要物理页号，而物理页号在并行排版阶段未知，所以总装只能放到最后。

> 关键结论：`finalize` 必须在并行排版之后、且必须串行，是因为它依赖**全局的物理页号**，而物理页号是一个随着每产出一张页面才递增的串行状态。

#### 4.1.2 核心流程

`finalize` 的执行过程可以分成五个阶段：

```
输入: LayoutedPage（半成品）+ 全局 counter + 全局 tags

① 判定并互换边距
   swap = binding.swap(counter.physical())      # 用物理页号判定
   if margin_two_sided && swap: 互换 margin.left/right
   if bleed_two_sided  && swap: 互换 bleed.left/right

② 建整页容器
   frame = Frame::hard(inner.size() + margin.sum_by_axis())   # 含边距、不含出血
   把暂存的 tags 全部 push 到 frame 原点

③ 按「前-中-后」顺序拼装 marginal + inner
   background（出血原点）→ header（左边距）→ inner（左上边距）
                        → footer（左、底）→ foreground（出血原点）

④ 计数器结算
   counter.visit(frame)   # 扫描本页内的 counter(page) 更新，更新逻辑页号
   number = counter.logical()
   counter.step()         # 跨过一条页边界：物理 +1、逻辑 +1

输出: Page { frame, bleed, fill, numbering, supplement, number }
```

注意「物理页号」和「逻辑页号」在这里第一次被分开使用：**互换边距用的是物理页号**（`counter.physical()`），**写进 `Page.number` 的是逻辑页号**（`counter.logical()`）。两者的区别在 4.3 详谈。

#### 4.1.3 源码精读

先看 `finalize` 的签名，它接收三个「跨页串行状态」——`counter`、`tags` 都以 `&mut` 传入，并用解构把 `LayoutedPage` 的所有字段一次性拆出来：

[finalize.rs:12-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L12-L31) —— `finalize` 的入参：`engine`、可变的 `counter` 与 `tags`，以及解构出的 `LayoutedPage` 全部字段（`inner`、`margin`/`bleed` 及其 `two_sided` 标记、`binding`、四个 marginal、`fill`、`numbering`、`supplement`）。

**① 互换边距（4.2 会展开 `swap` 的判定）**：

[finalize.rs:35-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L35-L41) —— 用物理页号算出 `swap`；只有当该侧（margin / bleed）本身是双面（`*_two_sided`）且 `swap` 为真时，才用 `std::mem::swap` 互换左右。注意 `margin` 和 `bleed` 用 `mut` 绑定，正是为了这里能原地互换。

**② 建整页容器并灌入暂存 tags**：

[finalize.rs:44-49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L44-L49) —— 用 `Frame::hard` 建一张「硬」尺寸的整页容器，尺寸 = 正文尺寸 + 左右边距 + 上下边距（`margin.sum_by_axis()` 分别把水平、垂直方向的边距求和）。注意这里**不含 bleed（出血）**——bleed 是导出时才在四周额外加的，见 4.1.4。然后把 `tags` 里暂存的标签全部 `drain` 出来 push 到原点。

> 这里的 `tags` 是 `layout_pages` 跨页维护的：u3-l2 讲过，`collect` 会产出 `Item::Tags`（纯标签碎片），它们在 `layout_pages` 循环里被攒进这个 `tags` 向量，到了下一张真实页面的 `finalize` 时被「补登」到页面开头。详见 [mod.rs:221-228](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L221-L228)。

**③ 按 background → header → inner → footer → foreground 拼装**：

[finalize.rs:51-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L51-L73) —— 拼装顺序。`bleed_origin = (-bleed.left, -bleed.top)` 让 background / foreground 覆盖到出血区域；header 放在 `(margin.left, 0)`；inner 放在 `(margin.left, margin.top)`；footer 放在 `(margin.left, 页高 - footer高)`；foreground 同样用出血原点。顺序就是注释里强调的「影响内省元素相对顺序、进而影响计数器解析」的那个顺序。

**④ 计数器结算并产出 `Page`**：

[finalize.rs:76-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L76-L82) —— 先 `counter.visit(frame)` 让计数器吸收本页内的 `counter(page)` 更新（更新逻辑页号），再取 `counter.logical()` 作为本页 `number`，最后 `counter.step()` 跨过页边界（物理、逻辑各 +1），返回最终 `Page`。

最后看 `layout_pages` 是怎么串行调用 `finalize` 的——它在一个 `for item in &items` 循环里，按 collect 产出的指令顺序，逐个把并行排好的 page run 或空白页（Parity 补页）交给 `finalize`：

[mod.rs:203-220](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/mod.rs#L203-L220) —— 三类 `Item` 的处理：`Run` 从并行结果 `runs` 里取出已排好的 `Vec<LayoutedPage>`，逐个 `finalize`；`Parity` 在需要补页时排一张空白页再 `finalize`；`Tags` 只把标签攒进 `tags`，不成页。`counter` 与 `tags` 在循环外声明、跨页共享，正是「串行」的体现。

#### 4.1.4 代码实践：跟踪一张页面的拼装坐标

**实践目标**：理解 `finalize` 如何用边距把 inner 与各 marginal 摆放到整页 frame 的正确坐标上，并弄清 bleed 为何不计入 frame 尺寸。

**操作步骤**：

1. 打开 [finalize.rs:43-73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L43-L73)。
2. 假设一张 A4 纸（210×297pt），正文 inner 尺寸 160×240pt，四周边距均为 25pt，bleed 均为 3pt。
3. 手算以下值：
   - 整页 frame 尺寸 `inner.size() + margin.sum_by_axis()`：水平 = 160 + 25 + 25 = 210pt，垂直 = 240 + 25 + 25 = 290pt。（注意：**不是** 297，因为这里不含 bleed。）
   - `bleed_origin`：`(-3, -3)`。
   - header 的 push 坐标：`(margin.left, 0) = (25, 0)`。
   - inner 的 push 坐标：`(margin.left, margin.top) = (25, 25)`。
   - footer 的 push 坐标：`(25, 290 - footer.height())`。
4. 思考：为什么 frame 尺寸不含 bleed？答案在 [document.rs:86-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L86-L88) 的字段注释——bleed 是「要在每侧额外附加、**不包含在 frame 内**」的量，留给 PDF/光栅导出器在裁切线外补出血用。`background`/`foreground` 用 `bleed_origin` 负坐标画出去，恰好覆盖到出血区。

**需要观察的现象 / 预期结果**：整页 frame 尺寸恒等于 `正文尺寸 + 边距`，bleed 完全独立于 frame；background 与 foreground 因使用 `bleed_origin` 而延伸到 frame 边界之外（负坐标区）。

> 待本地验证：若你想亲眼看到坐标，可在 `finalize.rs` 第 64 行（push inner）前后临时插入一条 `eprintln!("page inner at {:?}", Point::new(margin.left, margin.top));`，运行任意编译命令观察输出。本讲不假定你已运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `finalize` 里 push `inner`（第 64 行）和 push `footer`（第 67-70 行）的顺序对调，会对什么产生影响？

**参考答案**：会影响内省顺序与计数器解析。`counter.visit`（[finalize.rs:76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L76)）按 frame 内元素出现顺序扫描 `CounterUpdateElem`，而内省器也按该顺序定位元素。footer 里如果含有计数器更新或可内省元素，对调后它们会被排在 inner 内容之前，导致页码归属错乱。这正是注释 [finalize.rs:53-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L53-L55) 强调顺序「important」的原因。

**练习 2**：为什么 `LayoutedPage` 要把 `margin_two_sided` / `bleed_two_sided` 这两个布尔标记一并存下来，而不是只存 `margin` / `bleed` 本身？

**参考答案**：因为互换与否取决于**两个条件同时成立**：「该侧确实配置成双面」AND「当前页号需要互换」。`margin` 只是当前（互换前）的值，它本身无法告诉你用户是否启用了双面边距——一个单面文档的左右边距可能恰好相等，但不应被互换。`two_sided` 标记正是从 `PageElem::margin` 的 `two_sided` 字段读出来的（见 [run.rs:123](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L123)），必须在并行阶段就保存，留到 `finalize` 与 `swap` 一起做决定。

---

### 4.2 `Binding`：左右页边距互换的判定

#### 4.2.1 概念说明

`Binding`（装订方向）回答一个问题：这本书是从**左边装订**（LTR 语言习惯，如中文、英文书）还是从**右边装订**（RTL 语言习惯，如阿拉伯文、希伯来文书）？

装订方向决定了「哪一面是内边距（靠近书脊）、哪一面是外边距（靠近纸边）」。在双面打印时，奇数页和偶数页的内外边距是镜像的，所以需要根据物理页号决定是否把 `margin.left` 与 `margin.right`（以及 `bleed.left` 与 `bleed.right`）互换。

#### 4.2.2 核心流程

判定的核心是一个纯函数 `Binding::swap`，输入物理页号，输出一个布尔值「要不要互换」：

```
左装订 Binding::Left：在偶数页互换（因为第 1 页天然正确，无需互换）
右装订 Binding::Right：在奇数页互换（因为第 1 页天然错误，需要互换）
```

直觉解释：先固定「第 1 页不互换」是正确的情况——那就是左装订（封面在右、书脊在左，第 1 页是右页，左边距=内边距，天然正确）。于是左装订只需在偶数页（第 2、4、6…页，它们是左页）互换。右装订正好相反，第 1 页是错的，所以要在奇数页互换。

`Binding` 的默认值由文字方向推导：LTR → `Left`，RTL → `Right`，见 [run.rs:150-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L150-L155)。

#### 4.2.3 源码精读

`Binding` 枚举只有两个变体：

[page.rs:724-731](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L724-L731)（位于 `typst-library`）—— `Binding { Left, Right }`，注释说明 `Left` 是 LTR 习惯、`Right` 是 RTL 习惯。

判定逻辑全在 `swap`：

[page.rs:733-744](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/page.rs#L733-L744) —— `swap(number)`：`Left` 当页号为偶数时返回真；`Right` 当页号为奇数时返回真。注释点出了「因为第 1 页正确 / 错误」的设计依据。

调用点在 `finalize`，注意它喂给 `swap` 的是 `counter.physical()`（物理页号，从 1 开始的 `NonZeroUsize`）：

[finalize.rs:35-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L35-L41) —— `let swap = binding.swap(counter.physical());`，随后对 `margin`、`bleed` 分别做「`two_sided` 且 `swap`」才互换的条件判断。

#### 4.2.4 代码实践：解释 `binding.swap(counter.physical())`

**实践目标**：把本讲要求的实践任务——「解释 `binding.swap(counter.physical())` 如何决定 left/right 互换」——走一遍真实数字。

**操作步骤**：

1. 假设一篇左装订（`Binding::Left`，LTR 默认）、双面边距（`margin_two_sided = true`）的文档，正文配置 `inside = 30pt`、`outside = 20pt`。在 `run.rs` 阶段（未互换），假设 `margin.left = 30`（inside）、`margin.right = 20`（outside）。
2. 逐页推演 `finalize` 第 35-38 行：
   - **第 1 页**：`counter.physical() = 1`，`Binding::Left.swap(1)` → `1 % 2 == 0`？否 → `swap = false`，不互换。`margin.left = 30`（inside 靠书脊，第 1 页是右页，书脊在左，正确）。
   - **第 2 页**：`counter.physical() = 2`，`Left.swap(2)` → `2 % 2 == 0`？是 → `swap = true`，互换后 `margin.left = 20`、`margin.right = 30`（第 2 页是左页，书脊在右，inside 现在在右侧 = 30，正确）。
   - **第 3 页**：`swap = false`，恢复 `margin.left = 30`。
3. 现在改成右装订（`Binding::Right`），重做第 1 页：`Right.swap(1)` → `1 % 2 == 1`？是 → `swap = true`，互换。这与「左装订第 1 页不互换」恰好镜像，对应注释里「第 1 页是错的，需要互换」。

**需要观察的现象 / 预期结果**：左装订在偶数页互换、右装订在奇数页互换，二者关于「第 1 页」恰好互补；只要 `two_sided` 为真且 `swap` 为真，`margin` 和 `bleed` 的左右两侧才会被 `std::mem::swap` 原地互换。

> 待本地验证：要亲眼确认，可临时在 `finalize.rs` 第 36 行前插入 `eprintln!("phys={} swap={} two_sided={}", counter.physical(), swap, margin_two_sided);`，编译一篇多页双面文档观察每页输出。

#### 4.2.5 小练习与答案

**练习 1**：一个单面文档（`margin_two_sided = false`）但碰巧 `margin.left == margin.right`，`finalize` 会互换吗？

**参考答案**：不会。互换条件是 `margin_two_sided && swap`（[finalize.rs:36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L36)）。`margin_two_sided = false` 直接短路，根本不计算互换。即使左右值相等，「不互换」在语义上也是正确的——单面文档没有内外边距之分。

**练习 2**：为什么 `swap` 用的是物理页号（`counter.physical()`）而不是逻辑页号（`counter.logical()`）？

**参考答案**：因为「这一页纸物理上是左页还是右页」取决于它在纸堆里的实际位置（第几张纸），与用户用 `counter(page) update` 改写的显示页码无关。比如用户把页码从 1 改成 10，这张纸仍然是文档的第 1 张纸、仍然是右页，边距互换不该因此改变。物理页号忠实反映「第几张纸」，正是 `swap` 需要的语义。

---

### 4.3 `ManualPageCounter`：逻辑页号与物理页号

#### 4.3.1 概念说明

`ManualPageCounter` 是一个**专门为页面布局服务的双轨页码计数器**。它同时跟踪两个值：

- **物理页号（physical）**：这张纸是文档的第几张，从 1 开始，每跨过一条页边界 `+1`，**不可被用户改写**。它反映的是「纸的真实顺序」。
- **逻辑页号（logical）**：用户看到的页码，默认也从 1 开始每页 `+1`，但**可以被 `counter(page) update` 改写**（比如跳号、重新从 1 开始）。它反映的是「用户想要的显示页码」。

`finalize` 用物理页号决定边距互换（4.2），用逻辑页号填写 `Page.number`（最终导出时显示给用户的页码）。这就是本讲要求弄清的「两者何时不一致」。

#### 4.3.2 核心流程

`ManualPageCounter` 的生命周期跟随整个 `layout_pages` 串行循环：

```
new()                        # physical = 1, logical = 1
  │
  ├─ 对每张页面:
  │    counter.visit(frame)   # 扫描本页 frame 内的 counter(page) 更新
  │                           #   → 只更新 logical；physical 不动
  │    number = counter.logical()   # 写进 Page.number
  │    counter.step()         # physical += 1, logical += 1（跨页边界）
  │
  └─ 循环结束
```

关键点：`visit` 只动 `logical`，`step` 同时动两者。所以当页面里出现 `counter(page) update` 时，逻辑页号会偏离物理页号；不出现时，两者同步递增、始终相等。

#### 4.3.3 源码精读

结构体定义，两个字段一一对应两条轨道：

[counter.rs:724-730](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L724-L730)（位于 `typst-library`）—— 注释点明它「同时跟踪物理和逻辑页码计数器」；字段 `physical: NonZeroUsize`（从 1 开始、不可为 0）、`logical: u64`。

构造与两个读取方法：

[counter.rs:732-746](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L732-L746) —— `new()` 两者都初始化为 1；`physical()` / `logical()` 分别返回只读视图。

`visit` 是逻辑页号被改写的唯一入口——它递归遍历 frame（含子 group），找到所有 `CounterUpdateElem` 且 `key == CounterKey::Page` 的 start tag，套用更新：

[counter.rs:748-768](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L748-L768) —— `visit`：遇 `Group` 递归；遇 `Tag::Start(elem, _)` 若是 `CounterUpdateElem` 且 `key == Page`，则用当前 `self.logical` 构造 `CounterState`、执行 `update`、把结果回写 `self.logical`。**全程不动 `self.physical`**。

`step` 跨过页边界，两条轨道同步前进：

[counter.rs:770-774](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L770-L774) —— `step()`：`physical` 用 `saturating_add(1)`（防溢出）、`logical` 直接 `+= 1`。

最后回到 `finalize`，看这两个值如何分别被消费：

[finalize.rs:76-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L76-L80) —— `counter.visit(engine, &frame)?`（更新逻辑页号）→ `let number = counter.logical();`（取逻辑页号填 `Page.number`）→ `counter.step();`（跨页边界，两轨各 +1）。注意第 35 行的 `counter.physical()` 在此之前已被用于边距互换判定。

`Page.number` 的字段注释进一步确认它存的是逻辑页号：

[document.rs:102-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L102-L104) —— 注释明确：`number` 是「逻辑页号，受 `counter(page)` 控制，可能与物理页号不一致」。

#### 4.3.4 代码实践：逻辑页号 vs 物理页号何时不一致

**实践目标**：把本讲要求的实践任务——「说明 `counter.logical()` 与 `counter.physical()` 的区别，何时二者不一致」——用具体场景推演清楚。

**操作步骤**：

1. 假设一篇 3 页文档，**没有任何 `counter(page)` 更新**。推演 `layout_pages` 循环里 `counter` 的状态：
   - 初始：`physical = 1, logical = 1`。
   - 第 1 页：`visit` 无更新 → `logical = 1`；`number = 1`；`step` → `physical = 2, logical = 2`。
   - 第 2 页：`number = 2`；`step` → `physical = 3, logical = 3`。
   - 第 3 页：`number = 3`。
   - 结论：全程 `logical == physical`。
2. 现在假设第 2 页里有 `#counter(page).update(10)`（把页码跳到 10）。重做第 2 页：
   - 进入第 2 页时：`physical = 2, logical = 2`。
   - `visit` 扫到该更新：以 `logical = 2` 为基础套用 `update(10)` → `logical = 10`。
   - `number = counter.logical() = 10`（这张纸显示页码 10）。
   - `step` → `physical = 3, logical = 11`。
   - 第 3 页：`number = 11`（页码继续从 10 往上加）。
   - 结论：从第 2 页起 `logical ≠ physical`（10 ≠ 2、11 ≠ 3）。但**边距互换仍按物理页号**：第 2 页 `physical = 2`，左装订仍互换，与显示页码 10 无关。

**需要观察的现象 / 预期结果**：没有 `counter(page)` 更新时，逻辑页号 == 物理页号，同步递增；一旦出现更新，逻辑页号从更新点开始偏离，且后续页继续基于新值累加；而边距互换、`binding.swap` 始终只看物理页号，不受影响。这就是「两者不一致」的唯一来源——用户对 `counter(page)` 的改写。

> 待本地验证：可写一段 Typst 源码 `#counter(page).update(10)` 插在第二页，编译后查看第二、三页的页脚页码与装订边距，验证「显示页码跳变但边距互换规律不变」。

#### 4.3.5 小练习与答案

**练习 1**：`counter.visit` 为什么要递归处理 `FrameItem::Group`？

**参考答案**：因为 `counter(page) update` 可能出现在页眉、页脚、脚注、表格单元格等任何嵌套子 frame 里，而它们都被包成 `Group` 压进整页 frame。`visit` 必须递归进每个 group 才不会漏掉这些更新（[counter.rs:752](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L752)）。这也和 4.1 讲的「压入顺序影响内省」呼应——更新出现的先后顺序决定 `logical` 的演化路径。

**练习 2**：`physical` 用的是 `NonZeroUsize` 且 `step` 用 `saturating_add`，而 `logical` 用普通 `u64` 且 `step` 用 `+=`。为什么两者类型与运算不同？

**参考答案**：`physical` 表示「第几张纸」，语义上从 1 开始、永不为 0，用 `NonZeroUsize` 在类型层面固化这一不变量，正好契合 `Binding::swap(number: NonZeroUsize)` 的签名；用 `saturating_add` 在极端情况（极多页）下饱和而非溢出回绕，避免「回绕成 0 / 1」破坏装订判定。`logical` 是用户可控的显示值，可能被 `update` 设成任意值，用普通 `u64` 更宽松，`+=` 即可（实际页码远不会触达 `u64` 上限）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**端到端的串行总装推演**。

**场景**：一篇右装订（`set page(binding: right)`，对应 RTL 文档）、双面边距（`margin: (inside: 30pt, outside: 20pt)`）、且第 2 页含有 `#counter(page).update(5)` 的 3 页文档。假设 `run.rs` 阶段未互换时 `margin.left = 30`（inside）、`margin.right = 20`（outside）。

**任务**：填写下表（在草稿上手算）。

| 物理页号 | `Right.swap(phys)` | 互换后 `margin.left` | `visit` 后 `logical` | `Page.number` | `step` 后 `(phys, logical)` |
| --- | --- | --- | --- | --- | --- |
| 1 | ? | ? | ? | ? | ? |
| 2 | ? | ? | ? | ? | ? |
| 3 | ? | ? | ? | ? | ? |

**参考答案**：

| 物理页号 | `Right.swap(phys)` | 互换后 `margin.left` | `visit` 后 `logical` | `Page.number` | `step` 后 `(phys, logical)` |
| --- | --- | --- | --- | --- | --- |
| 1 | `1%2==1` → 真 | 互换 → 20 | 无更新 → 1 | 1 | (2, 2) |
| 2 | `2%2==1` → 假 | 不互换 → 30 | `update(5)` → 5 | 5 | (3, 6) |
| 3 | `3%2==1` → 真 | 互换 → 20 | 无更新 → 6 | 6 | (4, 7) |

**反思点**（把三个模块的结论对一遍）：

- **4.1**：每页都走了 `判互换 → 建整页 frame → 按 background/header/inner/footer/foreground 拼装 → visit → 取 logical → step` 的完整流程；`finalize` 串行执行，因为依赖跨页共享的 `counter` 与 `tags`。
- **4.2**：右装订在**奇数页**（1、3）互换，偶数页（2）不互换——注意第 2 页虽然逻辑页号被改写成 5，但互换判定只看物理页号 2，所以不互换。
- **4.3**：第 2 页起逻辑页号（5、6）与物理页号（2、3）不一致；`Page.number` 存逻辑页号（1、5、6），而边距互换用的是物理页号。

> 待本地验证：可用如下最小 Typst 源码本地编译，对照 PDF 检查每页页脚页码与左右边距是否符合上表：
>
> ```typst
> #set page(binding: right, margin: (inside: 30pt, outside: 20pt), numbering: "1")
> 第一页
> #pagebreak()
> 第二页 #counter(page).update(5)
> #pagebreak()
> 第三页
> ```
>
> （本讲不假定你已运行，标注为待本地验证。）

## 6. 本讲小结

- `finalize` 是 `layout_pages` 三段式的最后一段，负责把半成品 `LayoutedPage` 总装成最终 `Page`；它**必须串行**，因为它依赖跨页共享的物理页号（决定边距互换）与逻辑页号（决定显示页码）。
- 左右边距/出血的互换由 `binding.swap(counter.physical())` 决定：左装订在偶数页互换、右装订在奇数页互换，且只有当该侧 `two_sided` 为真时才生效。
- 页面拼装遵循固定顺序 `background → header → inner → footer → foreground`，这个顺序直接影响 `counter.visit` 与内省器对元素的解析顺序，不能随意调整。
- 整页 frame 尺寸 = 正文尺寸 + 边距（`margin.sum_by_axis()`），**不含 bleed**；bleed 是导出时在 frame 之外额外附加的出血量，`background`/`foreground` 用负的 `bleed_origin` 延伸覆盖。
- `ManualPageCounter` 维护物理、逻辑两条轨道：`visit` 只改逻辑页号（吸收 `counter(page)` 更新），`step` 两轨各 +1；`Page.number` 存逻辑页号，边距互换用物理页号。
- 逻辑页号与物理页号在没有 `counter(page)` 更新时始终相等；一旦出现更新，逻辑页号从更新点偏离，但边距互换仍只看物理页号——显示页码与「第几张纸」由此解耦。

## 7. 下一步学习建议

本讲完结了「页面与文档布局」对 `layout_pages` 三段式的拆解（u3-l1 到 u3-l4）。建议下一步：

- **学习 u3-l5「PagedIntrospector：构建查询索引」**：本讲反复提到「frame 的压入顺序决定内省顺序」，u3-l5 会讲清 `finalize` 产出的这些 `Page` 是如何被 `PagedIntrospector::new` 遍历、构建出 `query`/`counter`/`page_numbering` 等查询索引的。特别建议关注 `discover_frame` 如何处理带 `parent` 的 group——它解释了为什么 `finalize` 的 push 顺序如此重要。
- **回顾对照**：学完 u3-l5 后，回头重看本讲 [finalize.rs:53-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/finalize.rs#L53-L55) 的注释与 [counter.rs:748-768](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L748-L768) 的 `visit`，你会对「顺序为何重要」有更完整的认识。
- **延伸阅读**：进入 u4「流式（块级）布局」前，可以快速浏览 [run.rs:188-216](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/pages/run.rs#L188-L216)，看清 `inner`（本讲的输入）是如何由 `layout_flow(FlowMode::Root)` 产出的——这是页面内容侧与块级流布局的衔接点。
