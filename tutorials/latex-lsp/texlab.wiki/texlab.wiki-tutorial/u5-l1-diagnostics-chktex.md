# 诊断、chktex 集成与诊断过滤

## 1. 本讲目标

本讲聚焦 texlab 的「诊断（diagnostics）」子系统。学完本讲你应该能够：

- 说清 texlab 如何把外部 linter `chktex` 接入语言服务器，以及用哪三个配置项控制它「什么时候跑」「额外传什么参数」。
- 理解 `diagnosticsDelay` 作为防抖窗口的原理，并能权衡它对「响应快慢」与「性能开销」的影响。
- 用 `allowedPatterns`（白名单）和 `ignoredPatterns`（黑名单）这两组正则过滤诊断，并牢记「**白名单先于黑名单叠加**」的执行顺序。
- 知道一个容易踩的坑：`additionalArgs` 里**不要重定义** `-I` / `-f`，以及一条来自 u2-l1 的通用规则（flag 与参数必须拆成数组里的两个独立元素）继续生效。

> 本讲承接 [u2-l1 配置总览](u2-l1-config-overview.md)。我们会反复用到那里建立的「三要素」（Type / Default value / Placeholders）、「配置归客户端持有、texlab 查询」、「flag 与参数拆数组」这套语言，不再重复展开。

## 2. 前置知识

在动手前，先建立两个直觉。

**什么是「诊断」。** 在 LSP 里，诊断是服务器推给编辑器的一段「这里有问题」的信息——包含位置（文件、行、列）、严重等级（Error / Warning / …）和一条人类可读的消息。编辑器把它们渲染成代码里那一条条红色/黄色波浪线。texlab 推送诊断走的是标准方法 `textDocument/publishDiagnostics`。

**texlab 的诊断从哪里来。** texlab 自己会做一些轻量诊断（比如解析 LaTeX 源得到的语法/语义告警）；但它**不重新实现**风格检查，而是把这件事外包给老牌的 LaTeX linter [chktex](https://www.nongnu.org/chktex/)。这和 u1-l1 里讲的「texlab 是协调者」一脉相承：texlab 负责把外部命令拉进来、把输出翻译成 LSP 诊断。所以本讲其实是「**用配置驱动一个外部子进程，再解析它的输出**」这个通用模式的一个具体实例——和 `build.*`、`forwardSearch.*` 同构，只是这次外部命令是 `chktex`。

> 术语速查：
> - **chktex**：一个独立的 LaTeX 风格检查器（检查空格、标点、间距、`\ldots` 等排版习惯），不是 texlab 的一部分，需要你自行安装。
> - **publishDiagnostics**：LSP 服务器→客户端的「推送诊断」通知。
> - **防抖（debounce）**：频繁事件发生时，只在「安静一段时间」后才真正执行，避免每次按键都触发重活。
> - **白名单 / 黑名单**：白名单=只放行匹配的；黑名单=只放行不匹配的。

## 3. 本讲源码地图

本讲全部来自一个文件：[Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md)。

| 配置项（wiki 锚点） | 行号 | 作用 |
| --- | --- | --- |
| `texlab.chktex.onOpenAndSave` | L158–L166 | 文件**打开和保存**时跑 chktex |
| `texlab.chktex.onEdit` | L168–L176 | 文件**编辑**时跑 chktex（更频繁） |
| `texlab.chktex.additionalArgs` | L178–L187 | 给 chktex 额外传的命令行参数（禁改 `-I`/`-f`） |
| `texlab.diagnosticsDelay` | L189–L197 | 上报诊断前的防抖延迟（毫秒） |
| `texlab.diagnostics.allowedPatterns` | L199–L215 | 诊断白名单正则 |
| `texlab.diagnostics.ignoredPatterns` | L217–L229 | 诊断黑名单正则 |

> 注意命名上的小坑：`texlab.diagnosticsDelay` 是**扁平**命名（不在 `texlab.diagnostics.*` 子命名空间下），而 `allowedPatterns` / `ignoredPatterns` 才挂在 `texlab.diagnostics.*` 下。读配置时别想当然地写成 `texlab.diagnostics.delay`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **chktex 集成**——把外部 linter 接进来，控制它何时跑。
2. **diagnosticsDelay**——上报诊断前的防抖。
3. **诊断白/黑名单过滤**——`allowedPatterns` 与 `ignoredPatterns` 及其叠加顺序。

### 4.1 chktex 集成：把外部 linter 接入 texlab

#### 4.1.1 概念说明

chktex 是 texlab 之外的独立程序。texlab 在需要时把它当**子进程**拉起来，把它输出的文本告警**解析**成结构化的 LSP 诊断，再 `publishDiagnostics` 推给编辑器。

这套机制由三个配置项驱动，全部在 `texlab.chktex.*` 下，且默认值都是「关闭/空」：

| 配置项 | Type | Default | 含义 |
| --- | --- | --- | --- |
| `onOpenAndSave` | `boolean` | `false` | 打开、保存文件时各跑一次 chktex |
| `onEdit` | `boolean` | `false` | 每次编辑后再跑一次 chktex |
| `additionalArgs` | `string[]` | `[]` | 额外传给 chktex 的命令行参数 |

关键结论：**chktex 默认是关的**（两个触发开关都为 `false`）。即使你装了 chktex，不显式打开其中一个开关，texlab 也不会调用它——你只能看到 texlab 自身的轻量诊断。要让风格检查工作，必须把 `onOpenAndSave` 或 `onEdit` 至少一个设为 `true`。

`onOpenAndSave` 与 `onEdit` 的区别在于**频率与开销**：前者只在「打开、保存」两个低频事件触发，开销小；后者在「每次编辑」触发，更跟手但明显更费（chktex 要被反复拉起）。

#### 4.1.2 核心流程

把 chktex 当子进程驱动的流程（与 `build.*` 驱动 latexmk 同构）：

```text
事件（打开/保存/编辑）
   │
   ▼
[等待 diagnosticsDelay 防抖窗口]（见 4.2）
   │
   ▼
texlab 拼出 chktex 命令行：
   chktex  <texlab 内部固定的 -I / -f>  <additionalArgs 里的每一项>  <目标 .tex 文件>
   │
   ▼
运行子进程，捕获 chktex 的文本输出
   │
   ▼
texlab 按固定输出格式解析为 LSP 诊断（位置 + 消息 + 严重等级）
   │
   ▼
publishDiagnostics 推给编辑器（再经 allowedPatterns / ignoredPatterns 过滤，见 4.3）
```

注意三个要点：

- **两个固定 flag**：texlab 自己会塞进 `-I`（输入模式）和 `-f`（输出格式）这两个 chktex flag。`-f` 决定 chktex 每一行告警长什么样，texlab 依赖这个固定格式把文本切成「文件/行/列/消息」。
- **解析契约**：正因为 texlab 靠固定格式解析，如果你在 `additionalArgs` 里又塞了 `-I` 或 `-f`，就把格式改乱了，解析失败 → 诊断丢失或错位。
- **拆分数组**：`additionalArgs` 是 `string[]`，沿用 u2-l1 的规则——flag 和它的参数必须是数组里**两个独立元素**。比如想关掉第 22 号告警（chktex 的 `-n` flag），要写 `["-n", "22"]`，不能写成 `["-n 22"]`。

#### 4.1.3 源码精读

**触发开关一：打开/保存时检查** —— 布尔，默认 `false`：

[Configuration.md:L158-L166](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L158-L166) —— 定义 `onOpenAndSave`：在打开和保存文件后用 chktex 做检查。

**触发开关二：编辑时检查** —— 同样布尔、默认 `false`，但触发更频繁：

[Configuration.md:L168-L176](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L168-L176) —— 定义 `onEdit`：在编辑文件后用 chktex 做检查。

**额外参数与禁改约束** —— 这是本模块的重点：

[Configuration.md:L178-L187](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L178-L187) —— 定义 `additionalArgs`：传给 chktex 的额外命令行参数。关键一行是 wiki 里那句明确警告：

> Don't redefine the `-I` and `-f` flags as they are set by the server.

即：`-I` 和 `-f` 由服务器（texlab）自己设置，**不要重定义**。原因见 4.1.2 的「解析契约」。

#### 4.1.4 代码实践

**实践目标**：确认 chktex 默认关闭，并亲手把它打开。

**操作步骤**（源码阅读型 + 待本地验证）：

1. 准备一份能触发 chktex 告警的最小文档 `demo.tex`（示例代码，非项目原有文件）：

   ```tex
   \documentclass{article}
   \begin{document}
   Hello \TeX is nice.   % 触发 chktex 1: Command terminated with space
   \end{document}
   ```

   > `\TeX is` 这一行里，命令后的空格终止了 `\TeX`，chktex 会报第 1 号告警 *Command terminated with space*（建议改 `\TeX{} is`）。该告警编号与文案以你本机 chktex 版本为准，**待本地验证**。

2. 先**不**写任何 chktex 配置打开该文件，观察编辑器：默认 `onOpenAndSave=false`、`onEdit=false`，这条 chktex 告警应当**不出现**。

3. 加入最小配置（以编辑器配置 JSON 为例，字段沿用 texlab 三要素）：

   ```jsonc
   {
     "texlab.chktex.onOpenAndSave": true,   // boolean, default false
     "texlab.chktex.additionalArgs": ["-q"] // string[], default []; -q = chktex 安静模式
   }
   ```

   保存后重新打开 / 保存文件。

**需要观察的现象**：步骤 2 没有告警；步骤 3 之后 *Command terminated with space* 出现在编辑器里。

**预期结果**：证明「chktex 默认关闭，打开开关后 texlab 才会调用它」。

**无法确定的部分**：`-q` 主要减少 chktex 到 texlab 日志的噪声，不一定改变编辑器中可见的诊断列表，**待本地验证**。若你的 chktex 版本对 `\TeX is` 不报第 1 号，请换一个你本地能稳定触发的告警再做本实践。

#### 4.1.5 小练习与答案

1. **Q**：为什么不要在 `additionalArgs` 里重定义 `-I` 和 `-f`？
   **A**：texlab 自己设置了这两个 flag，其中 `-f` 决定 chktex 每行输出的固定格式，texlab 靠这个格式把文本解析成结构化诊断。你重定义就会破坏这个「解析契约」，导致诊断丢失或位置错乱。

2. **Q**：想给 chktex 传 `-n 22`（关闭第 22 号告警），`additionalArgs` 应该怎么写？
   **A**：`["-n", "22"]`——flag 和它的参数必须拆成数组里两个独立元素（沿用 u2-l1 的拆分规则），不能写成 `["-n 22"]`。

3. **Q**：`onOpenAndSave` 和 `onEdit` 都设为 `true` 时，chktex 会在什么时机运行？
   **A**：打开文件时、保存文件时各跑一次（`onOpenAndSave`），并且每次编辑后再跑一次（`onEdit`）。二者叠加，且 `onEdit` 明显更频繁、开销更大。

---

### 4.2 diagnosticsDelay：诊断上报的防抖

#### 4.2.1 概念说明

`texlab.diagnosticsDelay` 是一个整数（毫秒），默认 `300`：上报诊断前的防抖延迟。

直觉：你连续敲键盘时，每一下编辑都意味着「可能要重新算一遍诊断」。但每次都立刻算又太贵（chktex 要反复拉起、全文扫描）。于是 texlab 用一个**防抖窗口**：从最后一次编辑起计时，窗口内若又编辑了就重新计时；窗口内没有新编辑，才真正算一遍并推送。

它管的是**所有诊断**，不只是 chktex 的——texlab 自身的诊断也走这个防抖（因为诊断统一通过 `publishDiagnostics` 推送，延迟统一施加）。

#### 4.2.2 核心流程

防抖时序：

```text
t=0     编辑 A
t=50    编辑 B   → 计时器重置为从 t=50 起
t=200   编辑 C   → 计时器重置为从 t=200 起
t=200 + diagnosticsDelay(=300) → 即 t=500 仍无新编辑
        → texlab 才计算诊断并 publishDiagnostics
```

设 \( d = \) `diagnosticsDelay`，\( t_k \) 为第 \( k \) 次编辑时刻，则推送时刻为「最后一个编辑之后 \( d \) 毫秒」：

\[ t_{\text{publish}} = t_{\text{last}} + d \]

权衡表：

| `diagnosticsDelay` | 响应 | 代价 |
| --- | --- | --- |
| 偏小（如 `50`） | 诊断几乎实时刷新 | chktex 在密集编辑时被频繁拉起，大文件可能卡 |
| 默认 `300` | 折中 | 通常够用 |
| 偏大（如 `2000`） | CPU 友好 | 诊断明显滞后，有「敲完半天才报错」的延迟感 |

#### 4.2.3 源码精读

[Configuration.md:L189-L197](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L189-L197) —— 定义 `diagnosticsDelay`：上报诊断前的延迟（毫秒）。Type 为 `integer`，Default value 为 `300`。

> 命名提醒：它是扁平的 `texlab.diagnosticsDelay`，**不是** `texlab.diagnostics.delay`。`texlab.diagnostics.*` 子命名空间下只有 `allowedPatterns` 与 `ignoredPatterns`（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：直观感受防抖窗口对「诊断刷新时机」的影响。

**操作步骤**（待本地验证）：

1. 确保已开启某个 chktex 触发开关（如 `onEdit=true`），并有一份能稳定触发告警的 `demo.tex`。
2. 把延迟调大做对照实验：

   ```jsonc
   { "texlab.diagnosticsDelay": 2000 }
   ```

3. 在编辑器里删除又恢复触发告警的那几个字符（比如把 `\TeX is` 改成 `\TeX  is` 又改回），**连续**敲几下后停手。
4. 观察从「最后一次敲键」到「告警波浪线更新」之间隔了多久。
5. 再把值改回 `300`，重复步骤 3，对比两次的等待时长。

**需要观察的现象**：`2000` 时，停手后约 2 秒诊断才更新；`300` 时几乎立即更新。

**预期结果**：印证 \( t_{\text{publish}} = t_{\text{last}} + d \)，加深对「防抖 = 最后一次编辑之后等 \( d \) 毫秒」的理解。

#### 4.2.5 小练习与答案

1. **Q**：把 `diagnosticsDelay` 调到 `0` 会怎样？调到 `5000` 又会怎样？
   **A**：`0`：编辑后几乎立即重算并上报，chktex 在密集编辑时跑得非常频繁，大文件可能拖慢；`5000`：诊断最多滞后 5 秒才更新，体验上有明显延迟感。

2. **Q**：`diagnosticsDelay` 只影响 chktex 的诊断吗？
   **A**：不是。它是上报**所有**诊断（texlab 自身的 + chktex 的）的防抖窗口。

3. **Q**：这个配置项的全名是 `texlab.diagnosticsDelay` 还是 `texlab.diagnostics.delay`？
   **A**：`texlab.diagnosticsDelay`（扁平命名）。`texlab.diagnostics.*` 子命名空间下只有 `allowedPatterns` 和 `ignoredPatterns` 两项。

---

### 4.3 诊断白/黑名单过滤：allowedPatterns 与 ignoredPatterns

#### 4.3.1 概念说明

chktex（以及 texlab 自身）可能产出很多诊断，有些你并不关心（比如你坚持用 `...` 而非 `\ldots`）。`texlab.diagnostics.*` 提供两组**正则**来过滤诊断：

| 配置项 | Type | Default | 语义 |
| --- | --- | --- | --- |
| `allowedPatterns` | `string[]` | `[]` | **白名单**：只放行匹配其中**至少一条**的诊断 |
| `ignoredPatterns` | `string[]` | `[]` | **黑名单**：只放行匹配其中**零条**的诊断 |

正则作用于诊断的文本内容（即编辑器里那条人类可读的消息，例如 `Command terminated with space.`）。匹配对象以本地行为为准，**待本地验证**；实践中按「消息文本」写正则通常是对的。

> 与 4.1 的对比：`additionalArgs` 里的 `-n <num>` 是**让 chktex 自己别产生**某号告警（源头抑制）；`ignoredPatterns` 是 chktex **照样产生**、由 texlab 在**推送前**过滤掉（客户端侧过滤）。两者都能让某类告警消失，但发生层不同。

#### 4.3.2 核心流程

设诊断为 \( d \)，白名单 \( A = \) `allowedPatterns`，黑名单 \( B = \) `ignoredPatterns`，\( \mathrm{match}(p,d) \) 表示正则 \( p \) 匹配 \( d \) 的消息。

**白名单**：若 \( A \) 非空，只有匹配某条 \( a \in A \) 的诊断进入下一步；若 \( A \) 为空，**放行全部**。

\[ \text{stage}_1(d) =
\begin{cases}
\text{true} & A = \varnothing \\
\exists\, a \in A.\ \mathrm{match}(a,d) & A \neq \varnothing
\end{cases}
\]

**黑名单**：从上一步幸存者中，再剔除匹配某条 \( b \in B \) 的诊断。

\[ \text{send}(d) = \text{stage}_1(d)\ \land\ \neg\exists\, b \in B.\ \mathrm{match}(b,d) \]

**叠加顺序（重点）**：wiki 明确写道——两者都设置时，**先**套白名单、**再**对结果套黑名单。也就是说黑名单只在「白名单幸存者」里做二次剔除。

```text
全部诊断
   │
   ▼  allowedPatterns（先）
[白名单]  仅保留匹配某条 a 的诊断（A 为空则全保留）
   │
   ▼  ignoredPatterns（后）
[黑名单]  再剔除匹配某条 b 的诊断
   │
   ▼
推送给编辑器
```

几条可直接推出的结论：

- 若一条诊断**没匹配任何** `allowedPatterns`，它在白名单这关就被滤掉，**根本到不了**黑名单检查。
- 若 `allowedPatterns` 为空，白名单等价于「放行全部」，过滤完全由 `ignoredPatterns` 决定。
- 若两者都为空（默认），**不过滤**，所有诊断都推送。

#### 4.3.3 源码精读

**白名单** —— 含叠加顺序的官方提示：

[Configuration.md:L199-L215](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L199-L215) —— 定义 `allowedPatterns`：只发送匹配**至少一条**模式的诊断。关键提示在 L207–L209：

> If both `allowedPatterns` and `ignoredPatterns` are set, then allowed patterns are applied first. Afterwards, the results are filtered with the ignored patterns.

即「白名单先、黑名单后」，对应 4.3.2 的公式。

**黑名单** —— 互补语义：

[Configuration.md:L217-L229](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L217-L229) —— 定义 `ignoredPatterns`：只发送匹配**零条**模式的诊断。注意这一节**没有**重复「叠加顺序」提示——因为顺序提示已经写在 `allowedPatterns` 一侧了，这也侧面说明叠加逻辑由白名单那一节定义。

#### 4.3.4 代码实践

**实践目标**：用 `ignoredPatterns` 屏蔽一类常见告警，并验证其余诊断照常显示。

**操作步骤**（待本地验证）：

1. 确保 `chktex.onEdit=true`（或 `onOpenAndSave=true`），并准备 `demo.tex` 同时触发**两类**不同告警，例如：

   ```tex
   \documentclass{article}
   \begin{document}
   Hello \TeX is nice.   % 告警 1: Command terminated with space
   Some text ... more.   % 另一类告警（如 ... 应为 \ldots，视版本）
   \end{document}
   ```

2. 不加过滤，确认编辑器里能看到**两**类告警。

3. 加入黑名单，用一段正则匹配第 1 类告警的消息文本：

   ```jsonc
   {
     "texlab.diagnostics.ignoredPatterns": ["Command terminated with space"]
   }
   ```

   > 这里 `"Command terminated with space"` 本身也是合法正则（字面匹配）。想更严格可以写成 `"^Command terminated"`。

4. 保存配置、重新触发检查（再编辑一次或重新保存）。

**需要观察的现象**：第 1 类告警（*Command terminated with space*）**消失**；另一类告警**仍然显示**。

**预期结果**：印证黑名单只剔除匹配项、不影响其他诊断。若想进一步验证叠加顺序，参见本讲综合实践第 4 步。

#### 4.3.5 小练习与答案

1. **Q**：设 `allowedPatterns=["Overfull"]`，`ignoredPatterns=["Underfull"]`。一条 `Underfull \vbox` 的诊断会被发送吗？
   **A**：**不会**。它没有匹配任何 `allowedPatterns`（只有 `"Overfull"`），在白名单这一关就被滤掉，根本到不了黑名单检查——这正是「白名单先于黑名单」的直接后果。

2. **Q**：设 `allowedPatterns=["Overfull"]`，`ignoredPatterns=["Overfull.*hbox"]`。一条 `Overfull \hbox (10pt)` 的诊断会被发送吗？
   **A**：**不会**。白名单通过（匹配 `"Overfull"`），但随后黑名单 `"Overfull.*hbox"` 作为正则也匹配（`.*` 跨过空格与反斜杠），于是在第二步被剔除。

3. **Q**：只设 `ignoredPatterns=["Underfull"]`，`allowedPatterns` 留空（默认 `[]`）。`Underfull \vbox` 与 `Overfull \hbox` 分别会被发送吗？
   **A**：`Underfull \vbox` **不发送**（匹配黑名单被剔除）；`Overfull \hbox` **发送**（白名单空 = 放行全部，且不匹配黑名单 `"Underfull"`）。

---

## 5. 综合实践

把三个模块串起来，搭建一条「**编辑→防抖→chktex→过滤→推送**」的完整可观测链路。

**实践目标**：在一个工程里同时调通 chktex 触发、防抖延迟与白/黑名单过滤，并亲眼看到「白名单先、黑名单后」的叠加效果。

**操作步骤**（待本地验证）：

1. **准备测试文档** `filter.tex`（示例代码，非项目原有文件）：

   ```tex
   \documentclass{article}
   \begin{document}
   \TeX is text.      % 告警 A: Command terminated with space
   \end{document}
   ```

   并故意保留一处另一类 chktex 告警（如 `...` 或你本地能稳定触发的任一条），记为告警 B。

2. **开启 chktex、设定防抖、配置过滤**（一份完整 JSON）：

   ```jsonc
   {
     // 模块 4.1：开启 chktex（编辑时检查）
     "texlab.chktex.onEdit": true,                 // boolean, default false
     "texlab.chktex.additionalArgs": ["-q"],       // string[], default []; 勿重定义 -I/-f

     // 模块 4.2：防抖窗口调到 500，便于肉眼观察
     "texlab.diagnosticsDelay": 500,               // integer, default 300

     // 模块 4.3：先用黑名单屏蔽告警 A
     "texlab.diagnostics.ignoredPatterns": ["Command terminated with space"] // string[], default []
   }
   ```

3. **验证黑名单**：编辑 `filter.tex` 后观察——告警 A 消失、告警 B 仍在；同时注意停手后约 0.5 秒（防抖）才更新。

4. **验证叠加顺序**：把过滤改成同时设白名单和黑名单：

   ```jsonc
   {
     "texlab.diagnostics.allowedPatterns": ["Underfull"], // 只放行 Underfull 类
     "texlab.diagnostics.ignoredPatterns": ["Overfull.*hbox"]
   }
   ```

   - 预期：告警 A、告警 B（假设都不是 `Underfull`）**都消失**——因为它们在白名单这关就被滤掉了，黑名单根本没机会作用。
   - 再造一条 `Underfull \hbox` 诊断：白名单放行（匹配 `Underfull`），但黑名单 `"Overfull.*hbox"` 不匹配它 → 应当**仍被发送**。换成能同时被白名单和黑名单命中的诊断（例如让黑名单也写成 `["Underfull"]`），则它在白名单通过、黑名单被剔除 → **不发送**。

5. **回归**：清空 `allowedPatterns` / `ignoredPatterns`（都设为 `[]`），确认所有诊断重新出现。

**需要观察的现象**：
- 步骤 3：黑名单只删它匹配的那一类。
- 步骤 4：白名单先收口、黑名单只在幸存者里二次剔除。
- 步骤 5：两者为空即不过滤。

**预期结果**：你能用配置项精确地「点名放行 / 点名屏蔽」某一类诊断，并且顺序始终是「白名单先、黑名单后」。

> 如果某一步现象与预期不符，最可能的原因是 chktex 版本不同导致告警文案/编号有差异——换一条你本地能稳定触发的告警再做。涉及具体告警文案的部分均**待本地验证**。

## 6. 本讲小结

- texlab 把外部 linter `chktex` 当子进程驱动、解析其输出为 LSP 诊断；`onOpenAndSave` / `onEdit` 控制它**何时**跑（二者默认都为 `false`，即 chktex 默认关闭），`additionalArgs` 传额外参数。
- `additionalArgs` 有两条铁律：**不要重定义** `-I` / `-f`（texlab 靠固定输出格式解析诊断）；flag 与参数必须拆成数组里**两个独立元素**（如 `["-n", "22"]`）。
- `diagnosticsDelay`（默认 `300` 毫秒，**扁平命名**）是上报**所有**诊断的防抖窗口，权衡的是「响应」与「性能」。
- `diagnostics.allowedPatterns` 是白名单（匹配至少一条才放行）、`diagnostics.ignoredPatterns` 是黑名单（匹配零条才放行），都是正则 `string[]`，默认 `[]`（不过滤）。
- 两者同时设置时，**白名单先套、黑名单后套**；没过白名单的诊断到不了黑名单检查。

## 7. 下一步学习建议

- 想了解「过滤的另一面」——把自定义环境纳入文档符号、并按正则过滤符号，读 [u5-l2 符号、补全、悬停与 Inlay Hints](u5-l2-symbols-completion-hover.md)，那里有同构的 `texlab.symbols.allowedPatterns` / `ignoredPatterns`（且支持**递归**过滤）。
- 想用另一种方式抑制诊断——把某个不含 LaTeX 代码的环境标记为 verbatim 以免被检查，读 [u5-l4 experimental 扩展点](u5-l4-experimental-extensions.md) 中的 `texlab.experimental.verbatimEnvironments`。
- 想回到「编辑器内呈现」的全景，可顺读 u5-l2；想重新审视诊断在整个 LSP 数据流中的位置，可回顾 [u4-l1 自定义 LSP 消息](u4-l1-custom-lsp-messages.md) 中标准 `publishDiagnostics` 与自定义请求的区别。
