# 可视化配置与 DSL 编辑器

## 1. 本讲目标

本讲聚焦 Semantic Router 管理面板里「编辑路由策略」的两块核心交互：**BuilderPage 可视化构建器**与 **DslEditorPage DSL 编辑器**，并讲透它们之间的**双向编辑**关系。读完本讲你应该能够：

1. 说清楚面板里一共有几种「编辑路由」的方式，各自编辑的是什么（DSL 文本？配置 YAML？）。
2. 理解为什么可视化表单和文本编辑器能「永远同步」——因为它们共享同一个状态源 `dslSource` 字符串。
3. 看懂「可视化 → 文本」这一方向走的是 TypeScript 字符串手术（`dslMutations`），而「文本/配置 → DSL」走的是浏览器内 WASM 编译器，两者不是同一条路。
4. 解释一次 DSL↔YAML 往返中会发生哪些**归一化（normalization）**：为什么导出再导入后文本可能「看起来变了」但语义没变。
5. 在面板里亲手用可视化方式新增一条 ROUTE，切到 DSL 模式看它生成的文本，再走一遍导入流程验证一致性。

## 2. 前置知识

本讲是面板单元（U13）的第三篇，承接 u13-l2（面板前端 React）与 u7-l1（DSL 语法与 AST）。阅读前请先建立以下概念：

- **DSL 是 config.yaml 的「人友好版」**：用 `SIGNAL / PROJECTION / MODEL / ROUTE / PLUGIN` 等块状语法描述路由策略，编译后等价于 `config.yaml` 里的 `routing` 段。详见 u7-l1、u7-l2。
- **AST（抽象语法树）**：DSL 文本经解析器解析后的结构化中间表示，`pkg/dsl` 里叫 `Program`。可视化构建器渲染的就是这棵树。
- **Monaco Editor**：VS Code 同款的浏览器代码编辑器内核（`@monaco-editor/react`），支持自定义语言、语法高亮、自动补全、错误波浪线、Quick Fix。
- **WASM（WebAssembly）**：面板把 Go 写的 `pkg/dsl` 编译器编译成 `signal-compiler.wasm`，在浏览器里跑，编辑过程**不需要请求后端**。
- **Zustand**：React 的轻量状态管理库。面板用一个全局 store `useDSLStore` 统一管理 DSL 文本、编译产物、诊断信息和编辑模式。
- **ExtProc / 路由配方**：DSL 编译出的 YAML 最终会被部署到路由器进程，成为请求处理主链路（U5）使用的 `routing` 配置。

一个关键直觉先记住：**在面板里，DSL 文本字符串 `dslSource` 是唯一真相源（single source of truth）**。可视化、DSL、自然语言三种编辑模式，读的都是它、写的也都是它。

## 3. 本讲源码地图

本讲涉及的关键文件（全部位于 `dashboard/frontend/src/` 下，另有一个 Go 的 WASM 入口）：

| 文件 | 作用 |
| --- | --- |
| [`pages/BuilderPage.tsx`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/BuilderPage.tsx) | 可视化构建器主页：三模式（dsl/visual/nl）切换的壳，编排工具栏、编辑区、输出面板、导入与部署弹窗。 |
| [`pages/builderPageVisualShell.tsx`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/builderPageVisualShell.tsx) | 可视化模式的实体侧边栏（Models/Signals/Projections/Routes/Plugins）与主面板（表单/详情）。 |
| [`pages/DslEditorPage.tsx`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/DslEditorPage.tsx) | DSL 文本编辑器：基于 Monaco，提供语法高亮、补全、诊断、Quick Fix、编译输出。既能独立成页，也能嵌入 BuilderPage。 |
| [`pages/ConfigPageRecipePolicyEditor.tsx`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/ConfigPageRecipePolicyEditor.tsx) | 配置页里的「配方级策略信号」编辑器，直接改 YAML 结构（metadata / classifier 信号），是与 DSL 构建器对照的另一种编辑面。 |
| [`stores/dslStore.ts`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/stores/dslStore.ts) | Zustand 全局 store：托管 `dslSource`、编译/校验/解析动作、可视化增删改、导入与部署。是双向编辑的枢纽。 |
| [`lib/dslMutations.ts`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslMutations.ts) | 「可视化 → 文本」方向的纯函数：用正则 + 花括号计数定位块，做字符串手术与序列化。 |
| [`lib/dslLanguage.ts`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslLanguage.ts) | Monaco 的 DSL 语言定义：Monarch 词法、上下文补全、诊断 → 标记、Quick Fix。 |
| [`lib/wasm.ts`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/wasm.ts) | WASM 桥：加载 `signal-compiler.wasm`，暴露 `compile/validate/parseAST/decompile/format` 五个类型化方法。 |
| [`types/dsl.ts`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/types/dsl.ts) | DSL 相关 TypeScript 类型：`EditorMode`、`ASTProgram`、`BoolExprNode`、各 WASM 结果结构。 |
| [`src/semantic-router/cmd/wasm/main_wasm.go`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/wasm/main_wasm.go) | WASM 的 Go 入口：把 `pkg/dsl` 的能力注册成 5 个 JS 全局函数。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 DSL 编辑器**、**4.2 可视化构建器**、**4.3 双向编辑与归一化**。

### 4.1 DSL 编辑器（DslEditorPage）

#### 4.1.1 概念说明

`DslEditorPage` 是一个基于 **Monaco Editor** 的 DSL 文本编辑器。它的核心价值是把「写路由策略」做得像写代码一样：有语法高亮、有按上下文智能的自动补全、有红色波浪线提示错误、有「一键修复」（Quick Fix），还能即时把 DSL 编译成 YAML/CRD 看结果。

它有一个关键设计：**既能独立成一个页面，也能被嵌入（embedded）到 BuilderPage 里**。当 `embedded` 为真时，它隐藏自己的工具栏、状态栏和输出面板，只保留编辑器本体——因为 BuilderPage 会提供自己的工具栏和输出面板。这通过两个 prop 控制：

[DslEditorPage.tsx:38-45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/DslEditorPage.tsx#L38-L45) 定义了 `embedded` 与 `hideOutput` 两个开关，分别控制「隐藏自带工具栏/状态栏」和「隐藏 YAML/CRD 输出窗格」。后面会看到，BuilderPage 的「dsl 模式」就是用 `<DslEditorPage embedded hideOutput />` 复用的同一个组件。

#### 4.1.2 核心流程

DslEditorPage 的运行流程是一条「浏览器内全本地」的管线：

1. **挂载即初始化 WASM**：`initWasm()` 异步加载 `signal-compiler.wasm`，期间编辑器上方显示 "Loading Signal Compiler…" 遮罩。
2. **注册 DSL 语言**：Monaco 实例就绪时，调用 `registerDSLLanguage` 注册自定义语言 `signal-dsl`，包括词法、补全、Quick Fix 提供者，并安装深色主题 `signal-dsl-dark`。
3. **敲键即编辑**：每次内容变化回调 `handleEditorChange`，把新文本写进 store 的 `dslSource`。store 会启动一个 300ms 防抖的自动校验（`validate`）。
4. **校验产出诊断**：WASM 的 `signalValidate` 返回诊断数组（error/warning/constraint 三级），通过 `diagnosticsToMarkers` 转成 Monaco 的标记，错误就以波浪线形式出现在编辑器里。
5. **按需编译**：按 `Ctrl+Enter`（或点 Compile 按钮）触发 `compile`，产出 YAML 与 CRD 显示在右侧输出窗格。

值得强调的是：**编辑和校验全程不请求后端**，全部由浏览器里的 WASM 完成，所以输入响应很快。后端只在「部署」和「从路由器加载配置」时才被调用。

#### 4.1.3 源码精读

**Monaco 语言注册**：编辑器挂载时，`handleEditorMount` 把符号表与诊断的实时访问器传给 `registerDSLLanguage`，使补全和 Quick Fix 能拿到当前最新的符号：

[DslEditorPage.tsx:158-183](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/DslEditorPage.tsx#L158-L183) 调用 `registerDSLLanguage(monaco, () => useDSLStore.getState().symbols, () => useDSLStore.getState().diagnostics)`。注意这里传的是**函数**（`() => ...`）而不是值——这样 Monaco 在用户每次触发补全时都会「现拉」store 的最新状态，而不是用挂载时的快照。

**键盘快捷键**：编辑器注册了两个动作，分别绑定编译与格式化：

[DslEditorPage.tsx:126-151](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/DslEditorPage.tsx#L126-L151) 注册 `Ctrl+Enter → compile` 与 `Ctrl+Shift+F → format`。编辑器顶部的欢迎注释（`DEFAULT_DSL`）也把这两个快捷键告诉用户，见 [DslEditorPage.tsx:16-36](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/DslEditorPage.tsx#L16-L36)。

**上下文感知补全**：`dslLanguage.ts` 的补全提供者会「向后扫描光标」判断当前在写什么，再给不同建议。例如光标在 `SIGNAL ` 后面就建议信号族（keyword/embedding/domain…），在 `WHEN` 表达式里就建议已声明的信号引用 `type("name")`：

[dslLanguage.ts:371-403](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslLanguage.ts#L371-L403) 的 `getCompletionContext` 用一组正则判断上下文（`signal-type` / `plugin-ref` / `algo-type` / `model-ref` / `when-expr`）。其中 `when-expr` 分支会把符号表里已声明的信号拼成 `keyword("urgent")` 这样的引用供选择，见 [dslLanguage.ts:490-516](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslLanguage.ts#L490-L516)。

**诊断 → 波浪线 + Quick Fix**：WASM 返回的诊断带 `line/column/level`，甚至带可执行的「修复建议」`fixes`。`diagnosticsToMarkers` 把它们映射成 Monaco 标记：

[dslLanguage.ts:538-565](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslLanguage.ts#L538-L565) 把诊断转成 `IMarkerData`；当诊断带 `fixes` 时，还会把修复信息编码进 `relatedInformation`，供 [dslLanguage.ts:582-640](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslLanguage.ts#L582-L640) 的 CodeAction 提供者消费，最终在编辑器里出现「Fix」灯泡按钮。

**WASM 桥**：所有编辑能力背后是同一个 WASM 模块。`wasm.ts` 在初始化时把 Go 编译器跑起来，并等待 5 个全局函数注册完毕：

[wasm.ts:270-301](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/wasm.ts#L270-L301) 暴露的 `compile/validate/parseAST/decompile/format` 五个方法，本质都是调用 `window.signalXxx(dsl)` 再 `JSON.parse` 结果。为加快二次访问，它还用 IndexedDB 缓存编译好的 `WebAssembly.Module`，并通过 ETag 判断是否需要重新下载，见 [wasm.ts:158-227](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/wasm.ts#L158-L227)。

**Go 侧入口**：这 5 个全局函数由 `cmd/wasm/main_wasm.go` 注册：

[main_wasm.go:80-89](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/wasm/main_wasm.go#L80-L89) 在 `main()` 里 `js.Global().Set(...)` 注册五个函数，末尾 `select {}` 让 Go 程序常驻。其中 `compile` 是「解析→校验→编译→输出 YAML/CRD」的完整管线，见 [main_wasm.go:93-152](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/wasm/main_wasm.go#L93-L152)。

#### 4.1.4 代码实践

**实践目标**：在 DSL 编辑器里手写一段最小 DSL，观察实时诊断与编译输出，体验「全本地」编辑。

**操作步骤**（待本地验证，需要先按 u1-l3 / u13-l2 把面板跑起来）：

1. 打开面板，进入 DSL Editor 页（或 Builder → 切到 DSL 模式）。
2. 清空编辑器，粘贴下面这段示例代码（改编自编辑器自带的 `DEFAULT_DSL`）：

   ```
   MODEL "gpt-4o" {
     modality: "text"
     capabilities: ["general", "reasoning"]
   }

   SIGNAL keyword math_terms {
     keywords: ["calculus", "algebra"]
     operator: "OR"
   }

   ROUTE math_route (description = "Route math queries") {
     PRIORITY 10
     WHEN keyword("math_terms")
     MODEL "gpt-4o"
   }
   ```

3. 观察编辑器底部状态栏与 Problems 面板：此时应显示 0 errors、Signals: 1、Routes: 1。
4. 故意把 `keyword("math_terms")` 改成 `keywod("math_terms")`（拼错），等待约 300ms。
5. 按 `Ctrl+Enter` 编译。

**需要观察的现象**：
- 步骤 4 后，问题面板出现一条 error，编辑器对应行出现红色波浪线；如果该错误带修复建议，会出现「Fix」按钮。
- 步骤 5 编译成功后，右侧输出窗格出现 YAML（含一条 `decisions` 条目）和可选的 CRD 标签页。

**预期结果**：DSL 编辑器在不请求后端的情况下完成「写→校验→编译」全流程，验证了浏览器内 WASM 闭环。

#### 4.1.5 小练习与答案

**练习 1**：DslEditorPage 的 `embedded` 和 `hideOutput` 两个 prop 分别影响什么？为什么 BuilderPage 要同时传 `embedded hideOutput`？

**答案**：`embedded` 隐藏 DslEditorPage 自带的工具栏和状态栏（因为 BuilderPage 会提供自己的工具栏 `BuilderToolbar` 与状态栏 `BuilderStatusBar`）；`hideOutput` 隐藏右侧 YAML/CRD 输出窗格（因为 BuilderPage 有自己的 `BuilderOutputPanel`）。同时传两个，是为了避免在 Builder 里出现「两套工具栏 / 两套输出面板」的重复 UI。

**练习 2**：为什么 `registerDSLLanguage` 接收的是 `() => symbols` 这样的函数，而不是直接传 `symbols` 对象？

**答案**：因为补全和 Quick Fix 需要反映**编辑过程中不断变化**的符号表。如果传快照值，Monaco 只会看到组件挂载那一刻的符号，之后新增的信号/模型无法被补全。传函数（惰性求值）让 Monaco 在每次触发补全时现拉 store 最新状态。

---

### 4.2 可视化构建器（BuilderPage / VisualMode）

#### 4.2.1 概念说明

`BuilderPage` 是面板里功能最强的路由编辑入口。它把编辑分成**三种模式**：

- **dsl 模式**：内嵌 DslEditorPage，直接编辑 DSL 文本（就是 4.1 讲的那个编辑器）。
- **visual 模式**：表单式可视化编辑，左侧是实体侧边栏（Models / Signals / Projections / Routes / Plugins），右侧是表单和详情。
- **nl 模式**：自然语言生成，用一段话描述需求，后端 LLM 生成 DSL 草稿（本讲不深入，重点是前两种）。

模式枚举定义在 [dsl.ts:326](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/types/dsl.ts#L326)：`type EditorMode = 'dsl' | 'visual' | 'nl'`。

可视化构建器的核心思想是：**不要求用户会写 DSL 语法**。用户在表单里填字段、勾选信号、选模型，构建器替他把这些操作翻译成 DSL 文本。但它绝不维护一份「可视化的私有数据结构」——所有改动最终都落到同一个 `dslSource` 字符串上，与 DSL 模式共享。这就是它能和文本编辑器无缝切换的根本原因。

#### 4.2.2 核心流程

可视化模式的「读」方向（DSL → 界面）：

1. `dslSource` 变化时，调用 WASM 的 `parseAST()` 把文本解析成 `ASTProgram`（结构化、带位置信息）。
2. `VisualMode` 组件拿 `ast` 渲染侧边栏：每个实体类型一个可折叠分组，列出该项的名字与类型/优先级。
3. 点某个实体，从 `ast` 里 `find` 出对应节点，渲染到右侧详情/编辑表单。

可视化模式的「写」方向（界面 → DSL）走的是 **`dslMutations.ts` 的字符串手术**，而不是 WASM：

1. 用户在表单里提交改动（如新增 ROUTE），调用 store 的 `addRoute(name, input)`。
2. store 调用 `dslMutations.addRoute(dslSource, name, input)`，这是一个**纯函数**：用正则找到最后一个 ROUTE 块的位置，把新块文本拼接进去，返回新字符串。
3. store 用新字符串 `set({ dslSource: newSrc })`，并立即 `parseAST()` 刷新界面。
4. 因为 `dslSource` 变了，切到 dsl 模式时，Monaco 编辑器自然显示最新文本。

这条流程的关键点会在 4.3 详细展开：**可视化改动绕过了 WASM 编译器，直接改文本**。

#### 4.2.3 源码精读

**模式切换壳**：`BuilderPage` 用 `mode` 决定渲染哪个编辑区，三段并列的条件渲染：

[BuilderPage.tsx:569-631](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/BuilderPage.tsx#L569-L631) 中，`mode === "visual"` 渲染 `VisualMode`；`mode === "dsl"` 渲染 `<DslEditorPage embedded hideOutput />`（即复用 4.1 的编辑器）；`mode === "nl"` 渲染自然语言面板。注意 dsl 模式就是「嵌入的 DslEditorPage」，二者天然同步。

默认落地模式：组件挂载后强制设为 dsl 模式，[BuilderPage.tsx:144-147](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/BuilderPage.tsx#L144-L147)。

**首次进入自动加载路由器配置**：为了让用户一进来就有内容可编辑，BuilderPage 在 WASM 就绪后自动从路由器拉取当前 config 并反编译成 DSL：

[BuilderPage.tsx:447-485](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/BuilderPage.tsx#L447-L485) 用两个 ref（`autoLoadedDefaultConfigRef` / `autoLoadingDefaultConfigRef`）做幂等保护，避免重复加载；调用 `loadFromRouter()` 拉取 `/api/router/config/yaml`，再 `compile()` 产出 YAML。

**可视化侧边栏与表单**：`VisualMode` 把实体按类型组织成可折叠分组，每个分组都能「+」新增：

[builderPageVisualShell.tsx:265-479](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/builderPageVisualShell.tsx#L265-L479) 渲染 Models / Signals / Projection Partitions / Scores / Mappings / Routes / Plugins 七个 `SidebarSection`，每个都从 `ast` 读取条目并显示名字与标签（如 ROUTE 显示 `P{priority}`）。

**信号引用的展开**：可视化构建器在「可被 WHEN 引用的信号」列表里，对 complexity 信号做了特殊展开，对 mapping 投影的输出也当成可引用信号：

[builderPageVisualShell.tsx:145-162](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/builderPageVisualShell.tsx#L145-L162) 把 `complexity` 信号拆成 `:easy`/`:medium`/`:hard` 三个引用，把每个 `projectionMapping` 的 `outputs[].name` 当成 `projection` 类型的信号。这正是 u2-l3 提到的「mapping 产出以 `projection:名字` 反写进 SignalConfidences」在 UI 上的体现。

**新增 ROUTE 表单**：点 Routes 分组的「+」会切到 `AddRouteForm`，它提供名字、描述、优先级、WHEN 表达式构建器、模型、算法、插件等字段：

[builderPageVisualShell.tsx:523-530](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/builderPageVisualShell.tsx#L523-L530) 把 `availableSignals`（含上面的展开）、`availablePlugins`、`availableModels` 传给 `AddRouteForm`，其中 `ExpressionBuilder` 组件负责可视化拼 WHEN 布尔表达式。表单提交时（[builderPageAddRouteForm.tsx:54-74](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/builderPageAddRouteForm.tsx#L54-L74)）会把名字里的空格替换成下划线，组装成 `RouteInput` 交给 `onAdd`。

**CRUD 入口**：BuilderPage 把每种实体的增删改回调桥接到 store，例如新增路由：

[BuilderPage.tsx:326-333](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/BuilderPage.tsx#L326-L333) 的 `handleAddRoute` 调用 store 的 `addRoute`，并把当前选中设为新路由。store 侧的实现见 [dslStore.ts:420-425](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/stores/dslStore.ts#L420-L425)：调用 `addRouteMut` 改文本、置 `dirty`、再 `parseAST` 刷新。

#### 4.2.4 代码实践

**实践目标**：用可视化方式新增一条 ROUTE，体会「表单 → DSL 文本」的自动翻译。

**操作步骤**（待本地验证）：

1. 进入 Builder，切到 **Visual** 模式。
2. 在 **Routes** 分组点「+」，进入 AddRouteForm。
3. 填写：Name = `demo_qa`、Description = `Demo route`、Priority = `50`、WHEN 表达式选一个已存在的 keyword 信号（若无，先在 Signals 分组加一个 `SIGNAL keyword demo_kw { keywords: ["hi"] }`）、Model 填一个已声明的模型名。
4. 提交。侧边栏 Routes 出现 `demo_qa (P50)`。
5. **切到 DSL 模式**，在编辑器里找到新生成的 `ROUTE demo_qa (...) { ... }` 块。

**需要观察的现象**：
- 切到 DSL 模式后，文本里出现了结构规整的 ROUTE 块，包含 `PRIORITY 50`、`WHEN keyword("demo_kw")`、`MODEL "..."`。
- 名字里的空格被替换成了下划线。
- 如果你在 DSL 模式里手动改了这条 ROUTE 再切回 Visual，侧边栏与详情会反映你的修改（因为都来自同一个 `dslSource`）。

**预期结果**：可视化表单的提交被忠实地翻译成了 DSL 文本，且两个模式互相同步。

#### 4.2.5 小练习与答案

**练习 1**：可视化构建器渲染侧边栏用的是哪个数据结构？它是怎么来的？

**答案**：用的是 `ASTProgram`（`ast`），由 WASM 的 `signalParseAST(dslSource)` 生成。它比 `SymbolTable`（仅名字列表）更全——带有每个实体的字段、位置、子结构，所以能渲染详情表单。

**练习 2**：BuilderPage 一进来就处于哪个模式？为什么它还要在后台偷偷从路由器拉配置？

**答案**：默认 dsl 模式（[BuilderPage.tsx:144-147](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/BuilderPage.tsx#L144-L147)）。后台拉配置（`loadFromRouter`）是为了让编辑器一打开就有「当前线上正在用的路由策略」可编辑，而不是空白——这是一种「所见即线上」的产品设计。

---

### 4.3 双向编辑与归一化（Bidirectional Editing & Normalization）

#### 4.3.1 概念说明

「双向编辑」是本讲最核心的结论，可以浓缩成一张心智图：

```
                       ┌─────────────────────────────────────┐
                       │   dslSource（DSL 文本，唯一真相源）  │
                       └─────────────────────────────────────┘
            读(parseAST) ↑  ↓ 写(dslMutations 字符串手术)         ↑ setDslSource
        ┌────────────────┘  └────────────────┐          ┌─────────────────────┐
   visual / DSL 模式界面                 visual 表单提交        Monaco 手敲 / nl 草稿

                       ┌─────────────────────────────────────┐
                       │      WASM 编译器（浏览器内）         │
                       │  parseAST / validate / compile       │
                       │  decompile / format                  │
                       └─────────────────────────────────────┘
                              ↑ compile()            ↓ importYaml/decompile
                       ┌──────┴──────┐          ┌────┴─────────────────────┐
                       │ yamlOutput   │          │ YAML（来自路由器/文件/URL）│
                       │ （部署用）    │          │ 只取 routing 段反编译成 DSL │
                       └─────────────┘          └──────────────────────────┘
```

三个要点：

1. **`dslSource` 是唯一真相源**。visual、dsl、nl 三种模式共享它，所以无论从哪种模式改，另两种都立即一致。
2. **两个方向的实现路径不同**。「可视化 → 文本」是 TypeScript 字符串手术（`dslMutations`），**不走 WASM**；「文本 → 结构化视图」才走 WASM（`parseAST`）。换句话说，可视化改动的「翻译器」是前端写的，不是编译器。
3. **DSL 与 YAML 不在同一个层面**。DSL 只描述 `routing` 段；完整的 config.yaml 还包含 `global / providers / listeners / entrypoints` 等基础设施。所以「导入 YAML」只把 routing 反编译进 DSL，其余存进 `baseConfigYaml` 在部署时合并回去。

第三个文件 `ConfigPageRecipePolicyEditor.tsx` 属于**另一种编辑面**：它位于配置页（ConfigPage，见 u13-l2），直接以表单编辑 YAML 结构里的 `ConfigSignals`（metadata / classifier 信号），改动通过 `onChange` 命令式地改配置对象，**完全不经过 DSL 与 WASM**。它和 BuilderPage 代表了面板里两种不同的编辑哲学：Builder 是「DSL 中心、可往返」，ConfigPage 是「YAML 直改、配方局部」。

#### 4.3.2 核心流程

**A. 可视化 → 文本（dslMutations）**

每一次可视化增删改，都对应一个纯函数，流程统一为「定位块 → 字符串拼接 → 返回新文本」：

1. `findBlock(src, construct, subType, name)` 用正则匹配块头（如 `^ROUTE\s+name\s*(?:\(...\))?\s*{`），再用**花括号计数**找到配对的闭合 `}`。
2. 序列化字段：`serializeFields` / `serializeValue` 把 JS 对象写回 DSL 文本。
3. 拼接：用 `src.slice(0, start) + newBlock + src.slice(end)` 做外科手术式替换；删除时把 3 个以上连续空行折叠成 2 个。

**B. 文本 → 可视化（parseAST）**

WASM 的 `signalParseAST` 解析文本产出 `ASTProgram`（[dsl.ts:172-181](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/types/dsl.ts#L172-L181)），含 signals / projections / routes / models / plugins / testBlocks 各数组，每个节点带 `pos` 位置。store 在每次 `setDslSource` 后（可视化模式）触发它来刷新界面。

**C. 导入 YAML → DSL（decompile，routing-only）**

1. `loadFromRouter()` 拉取 `/api/router/config/yaml`。
2. `importYaml(yaml)` 调用 WASM `decompile`，Go 侧 `signalDecompile` 用 `config.ParseYAMLBytes` 解析整份配置，再 `dsl.DecompileRouting(cfg)` **只把 routing 段**反编译成 DSL。
3. 原 YAML 存进 `baseConfigYaml`，部署时与编译出的 routing YAML 合并。

**D. 编译 DSL → YAML（compile，部署前置）**

`compile()` 调用 WASM `signalCompile`，Go 侧用 `dsl.EmitRoutingYAMLFromConfig` 输出 routing 片段、用 `dsl.EmitCRD` 输出 CRD。这是「部署」按钮的前置步骤。

#### 4.3.3 源码精读

**字符串手术的核心：findBlock**：

[dslMutations.ts:27-90](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslMutations.ts#L27-L90) 的 `findBlock`：先用正则定位块头，再逐字符数 `{`/`}` 找到配对的闭合括号（[dslMutations.ts:62-83](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslMutations.ts#L62-L83)），返回 `{ start, end, body }` 区间。这是所有增删改的基础——它使得可视化编辑不需要理解 DSL 的完整语法，只需「找到同名块、整体替换」。

**字段序列化与小对象内联**：

[dslMutations.ts:199-231](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslMutations.ts#L199-L231) 的 `serializeValue` 有意模仿 Go 反编译器的输出风格：当对象是「全部基本类型且叶子字段 ≤ 3 个」时，内联写成 `{ a: 1, b: 2 }`（[dslMutations.ts:217-223](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslMutations.ts#L217-L223)），否则多行展开。这是为了让可视化生成的文本尽量贴近反编译器的「标准长相」，减少往返差异。

**ROUTE 的序列化**：

[dslMutations.ts:512-572](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslMutations.ts#L512-L572) 的 `serializeRouteBody` 按 `PRIORITY → WHEN → MODEL → ALGORITHM → PLUGIN` 固定顺序拼装路由体，每段之间空一行。注意 **PRIORITY 永远会被写出**（[dslMutations.ts:516](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslMutations.ts#L516)），即使原文没写——这是往返里一个典型的「无损但文本会变」的归一化。

**布尔表达式回写时的括号**：

[dslMutations.ts:638-653](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/lib/dslMutations.ts#L638-L653) 的 `serializeBoolExpr`：`OR` 节点会被包进括号 `(A OR B)`，而 `AND` 不会。这是因为 DSL 里 NOT > AND > OR 的优先级（u7-l1），给 OR 加括号是为了在回写时**保住语义**不被优先级改变。

**导入与反编译（routing-only）**：

[dslStore.ts:231-247](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/stores/dslStore.ts#L231-L247) 的 `importYaml` 调 `decompile`，失败抛错；成功则把 DSL 写入 `dslSource`、把原 YAML 存进 `baseConfigYaml`。Go 侧 [main_wasm.go:202-219](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/wasm/main_wasm.go#L202-L219) 的 `decompile` 调用 `dsl.DecompileRouting(cfg)`——名字里的 "Routing" 明确表示**只反编译 routing 段**。这就是导入提示语「only the routing section is imported into DSL」的来源（[BuilderPage.tsx:359-362](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/BuilderPage.tsx#L359-L362)）。

**格式化 = 往返归一化**：

[main_wasm.go:221-235](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/wasm/main_wasm.go#L221-L235) 的 `format` 注释明说「Canonical formatting via compile→decompile round-trip」——格式化本身就是「编译再反编译」一次，让文本落到标准形态。这也是为什么 `format` 后的 DSL 通常更规整。

**对照面：ConfigPageRecipePolicyEditor（YAML 直改）**：

[ConfigPageRecipePolicyEditor.tsx:40-65](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/ConfigPageRecipePolicyEditor.tsx#L40-L65) 直接编辑 `ConfigSignals`（`metadata` 与 `classifiers` 两类信号），通过 `onChange` 命令式更新配置对象，没有任何 DSL 或 WASM 参与。它的提示语 [ConfigPageRecipePolicyEditor.tsx:68-70](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/ConfigPageRecipePolicyEditor.tsx#L68-L70) 明说「These policy signals belong only to this recipe」——这些是配方局部信号。它的谓词有三种（[ConfigPageRecipePolicyEditor.tsx:25-38](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/ConfigPageRecipePolicyEditor.tsx#L25-L38)）：`equals` / `in` / `exists`。把它和 BuilderPage 对照看：Builder 走 DSL、可全量往返；RecipePolicyEditor 走 YAML、只管配方局部信号——两者解决不同粒度的编辑需求。

> 小结：一次「可视化新增 ROUTE → 切 DSL → 导出 YAML → 再导入 DSL」的往返里，你会观察到这些**归一化**：PRIORITY 总被写出、OR 表达式被加括号、小对象被内联、complexity 信号在可视化里被展开成三段、名字空格被替换成下划线、导入只保留 routing 段（global/providers/listeners 落到 baseConfigYaml）。这些都不会改变路由语义，但会让文本「看起来变了」。

#### 4.3.4 代码实践（本讲综合实践见第 5 节，此处给出导入侧的对照小实践）

**实践目标**：亲手走一遍「DSL → YAML → DSL」往返，记录归一化现象。

**操作步骤**（待本地验证）：

1. 在 Builder 的 DSL 模式里手写一条简单路由（含一个 OR 条件，且故意不写 PRIORITY）：
   ```
   SIGNAL keyword demo_kw { keywords: ["hi"] }
   ROUTE demo_route { WHEN keyword("demo_kw") OR keyword("other"); MODEL "gpt-4o" }
   ```
2. 点 Compile，把右侧 YAML 复制出来（或保存到本地文件）。
3. 点 Import YAML，把刚才的 YAML 粘进去确认。

**需要观察的现象**：导入回的 DSL 里，PRIORITY 被显式补上了；OR 表达式可能被加上括号；块与块之间的空行被规范化。

**预期结果**：文本发生了「无损归一化」，但路由语义（命中条件、模型）完全一致。这正是 u7-l2 总结的「YAML↔DSL 往返在路由语义层严格可逆，但在 DSL 文本层不逐字可逆」的可视化体现。

#### 4.3.5 小练习与答案

**练习 1**：可视化模式下，用户在表单里改了一个字段，这条改动经过 WASM 编译器了吗？

**答案**：写方向没有。可视化改动走的是 `dslMutations.ts` 的字符串手术，直接改 `dslSource`，再调 `parseAST`（这才用到 WASM）刷新界面。WASM 编译器只在 parseAST / validate / compile / decompile / format 这些「读」或「显式编译」时介入，可视化「写」路径绕开了它。

**练习 2**：为什么从路由器导入完整 config.yaml 后，DSL 里看不到 providers 和 global 段？

**答案**：因为 `signalDecompile` 调用的是 `dsl.DecompileRouting`，只反编译 routing 段（见 [main_wasm.go:213](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/wasm/main_wasm.go#L213)）。DSL 的设计职责就是描述路由策略（signals/projections/decisions），不承载基础设施；global/providers/listeners 被保留在 `baseConfigYaml` 里，部署时与编译出的 routing YAML 合并成完整配置。

**练习 3**：`ConfigPageRecipePolicyEditor` 编辑的信号和 Builder 里编辑的 SIGNAL 块是同一回事吗？

**答案**：不是同一套机制。Builder 编辑的是 DSL 的 `SIGNAL` 块（走 WASM、可全量往返）；`ConfigPageRecipePolicyEditor` 编辑的是配置 YAML 里配方局部的 `metadata` / `classifier` 策略信号（走 YAML 直改、`onChange` 命令式更新），它明确声明这些信号「只属于该 recipe」。两者是面板里不同粒度的编辑入口。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「可视化建模 → 双向校验 → 部署前预览」的完整闭环。

**任务**：用可视化构建器新增一条带布尔条件的 ROUTE，验证它在 DSL 与 YAML 两面的表现，最后（只到预览，不实际部署）观察部署差异。

**步骤**（待本地验证，需面板可访问）：

1. **准备信号与模型**：进入 Builder → Visual 模式。在 Signals 加一个 `keyword` 信号 `greet_kw`（keywords: `["hello", "hi"]`）；在 Models 确认至少有一个模型（若空，可加 `MODEL "demo-model" { modality: "text" }`）。
2. **可视化新增 ROUTE**：在 Routes 点「+」，填 Name = `greet_route`、Priority = `40`、WHEN 用 ExpressionBuilder 选 `keyword("greet_kw")`、Model 选刚才的模型。提交。
3. **切 DSL 校验**：切到 DSL 模式，定位 `ROUTE greet_route`，确认生成的文本含 `PRIORITY 40`、`WHEN keyword("greet_kw")`、`MODEL "demo-model"`。注意名字已规整、PRIORITY 已显式写出。
4. **手改再回切**：在 DSL 模式里把 WHEN 改成 `keyword("greet_kw") OR keyword("other")`，等 300ms 自动校验通过；切回 Visual，确认详情表单反映了新条件。
5. **编译并导出**：点 Compile，在输出面板切到 YAML 标签，复制生成的 routing YAML。
6. **往返验证**：点 Import YAML，把刚复制的 YAML 粘进去确认。对比导入后的 DSL 与步骤 3 的文本，记录归一化差异（OR 是否加括号、空行、PRIORITY 等）。
7. **部署预览（不实际部署）**：若有部署权限，点 Deploy 触发 `requestDeploy`，在确认弹窗里对比 `deployPreviewCurrent`（线上当前 YAML）与 `deployPreviewMerged`（合并后预览），观察你的改动会合并成什么样——然后取消，不点确认部署。

**验收点**：
- 步骤 3 能看到表单被正确翻译成 DSL；
- 步骤 4 证明两个模式双向同步；
- 步骤 6 列出至少 2 处归一化现象；
- 步骤 7 能说清楚预览面板里「current」与「merged」分别代表什么。

> 说明：步骤 7 涉及 `requestDeploy` 会调用 `/api/router/config/deploy/preview`（见 [dslStore.ts:463-487](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/stores/dslStore.ts#L463-L487)）。若你无部署权限或处于只读模式，Deploy 按钮会被禁用（[BuilderPage.tsx:180-181](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/BuilderPage.tsx#L180-L181)），此时跳过本步即可。

## 6. 本讲小结

- 面板有**两个主要的路由编辑入口**：BuilderPage（DSL 中心，含 dsl/visual/nl 三模式）与 ConfigPage 里的配方策略编辑器（YAML 直改，配方局部）。本讲重点是前者。
- **`dslSource` 是唯一真相源**：visual、dsl、nl 三模式共享同一个 DSL 文本字符串，所以任意模式改动都立即在其他模式可见。
- **两个方向路径不同**：「可视化 → 文本」走 `dslMutations.ts` 的正则 + 花括号计数字符串手术，**不经 WASM**；「文本 → 视图」走 WASM 的 `parseAST`。
- **DslEditorPage 基于 Monaco**，注册了自定义语言 `signal-dsl`，提供上下文补全、诊断波浪线、Quick Fix，并通过 `embedded`/`hideOutput` 两个 prop 被无缝嵌入 BuilderPage。
- **一切编辑能力背后是同一个浏览器内 WASM 编译器**（`signal-compiler.wasm`，由 Go `pkg/dsl` 编译），暴露 `compile/validate/parseAST/decompile/format` 五个函数；编辑过程不请求后端。
- **DSL↔YAML 往返会发生无损归一化**：PRIORITY 总写出、OR 加括号、小对象内联、complexity 三段展开、名字规整、导入只取 routing 段（基础设施存进 `baseConfigYaml`）——语义严格可逆，但文本不逐字可逆。

## 7. 下一步学习建议

- **向下到编译器内核**：本讲只把 WASM 当黑盒。要理解 `compile/decompile/format` 内部如何把 AST 编译成 `RouterConfig` 再 emit YAML、如何反编译回 DSL，请读 u7-l2（DSL 编译与反编译）和 `pkg/dsl/compiler.go` / `decompiler.go`。
- **横向到部署链路**：本讲的「Deploy」止步于预览。真正的部署（写盘 → 运行时热重载 → 健康轮询）发生在 `dslStore.executeDeploy` 与后端 apiserver，结合 u11-l1（API Server 管理 API）与 u13-l1（面板后端）一起读。
- **自然语言模式**：本讲略过了 `mode === "nl"`。若对「自然语言生成 DSL 草稿 + 校验 + 暂存」感兴趣，可读 `BuilderNaturalLanguagePanel` 与 store 的 `generateFromNaturalLanguage` / `applyNaturalLanguageDraft`，它呼应了 u7-l3 的 `generate` 子命令的「schema-as-supervision」哲学。
- **可解释性闭环**：可视化编辑产出路由后，想看一条请求实际命中了哪条 ROUTE、各投影如何分解，可结合 u11-l2（投影追踪与可解释性）与面板里的请求/重放视图。
