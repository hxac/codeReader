# 前端启动流程：从 main.tsx 到 App.tsx

## 1. 本讲目标

本讲是「动手」系列的承接篇。上一讲（u1-l3）我们已经知道了怎么装依赖、跑命令、怎么看目录；这一讲我们要回答一个更具体的问题：

> 当你按下 `pnpm dev`、应用窗口弹出来的那一瞬间，**前端代码到底按什么顺序执行？是谁先跑、谁后跑？中间出错了怎么办？**

学完本讲，你应当能够：

1. 画出 cc-switch 前端从「HTML 入口 → main.tsx → App.tsx」的完整启动时序。
2. 说清楚 `bootstrap()` 这个异步函数为什么要先查后端、再渲染界面。
3. 识别并解释四层全局 Provider（`QueryClientProvider` / `ThemeProvider` / `UpdateProvider` / i18n）各自的职责。
4. 说出当后端配置加载失败时，前端「为什么不会渲染、而是弹窗后直接退出」。
5. **（本次新增）** 解释当 `get_init_error` 返回 `kind === "db_version_too_new"`（数据库版本过新）时，前端为什么**不退出**、而是改渲染一个应用内「升级应用」恢复界面 `DatabaseUpgrade`，并讲清它与普通 `handleConfigLoadError` 路径的差异。
6. 读懂 `App.tsx` 如何用一个 `View` 状态 + `renderContent()` 把几十个功能面板拼到一起。

---

## 2. 前置知识

本讲假设你已经读过 u1-l1～u1-l3，至少知道：

- cc-switch 是 **Tauri 2 桌面应用**，前端是 React 18，后端是 Rust。
- 前端和后端通过 **Tauri IPC** 通信：前端用 `invoke("命令名")` 调后端、用 `listen("事件名")` 监听后端推送。
- 项目采用 **SSOT（单一事实源）**：可同步数据都在后端 SQLite 数据库里，前端只是「展示层 + 操作入口」。

下面几个 React / 浏览器概念，本讲会用到，先一句话解释：

| 术语 | 一句话解释 |
|---|---|
| **入口文件（entry）** | 整个前端「最先被执行」的那个文件，通常是 `main.tsx`。浏览器/Vite 从它开始一路 `import` 把整棵代码树加载进来。 |
| **挂载（mount）** | 把 React 组件树「画」到网页上某个 DOM 节点（这里是 `<div id="root">`）的过程，靠 `ReactDOM.createRoot(...).render(...)` 完成。 |
| **Provider（提供者）** | React Context 的一种组件，写在组件树外层，让内层所有组件都能共享某份数据（比如「当前主题」「查询缓存」）。 |
| **副作用（side effect）** | `import` 一个模块时，模块顶层代码会立即执行一次。i18n 的初始化就是靠这种「导入即执行」完成的。 |
| **bootstrap（引导）** | 应用启动时那段「先做准备、再正式开跑」的代码。本讲的 `bootstrap()` 函数就是干这个的。 |
| **状态机（state machine）** | 一个变量（如 `phase`）只能在几个固定取值间切换，每次切换都对应一段明确的逻辑。本讲 `DatabaseUpgrade` 组件就是一个状态机。 |

> 小提示：如果你对 React 的 `useEffect` / `useState` 还不熟，本讲的 4.4 节会用到，但不影响你理解启动主线。可以先把它当作「组件挂载后自动跑一段逻辑」来理解。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下，建议对照源码阅读（所有链接均指向本次增量对应的 HEAD `edeee25f`）：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [src/index.html](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/index.html) | HTML 外壳 | 提供 `<div id="root">` 挂载点，并指向 `main.tsx` |
| [src/main.tsx](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx) | **前端入口** | bootstrap 时序、错误监听、`db_version_too_new` 分支、Provider 装配、挂载 App |
| [src/i18n/index.ts](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/i18n/index.ts) | 国际化配置 | 「导入即初始化」的语言探测 |
| [src/lib/query/queryClient.ts](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/lib/query/queryClient.ts) | TanStack Query 配置 | 全局数据缓存的默认行为 |
| [src/components/theme-provider.tsx](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/theme-provider.tsx) | 主题 Provider | 深浅色切换 + 同步原生窗口 |
| [src/contexts/UpdateContext.tsx](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/contexts/UpdateContext.tsx) | 更新 Provider | 启动后自动检查新版本 |
| [src/components/DatabaseUpgrade.tsx](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx) | **（本次新增）数据库版本过新恢复界面** | `db_version_too_new` 时渲染的自助升级界面 |
| [src/App.tsx](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx) | 主界面组件 | 状态中枢 + 面板路由 `renderContent()` |

后端只在本讲的「错误处理」里牵涉一点点，作为对照：

| 文件 | 作用 |
|---|---|
| [src-tauri/src/commands/misc.rs](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/commands/misc.rs) | 提供 `get_init_error` 命令，供前端在启动早期拉取初始化错误 |
| [src-tauri/src/init_status.rs](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs) | 用全局静态变量保存启动期产生的初始化错误（本次新增了 `kind`/`db_version`/`supported_version` 字段） |

---

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. **bootstrap 启动序列** —— 代码从哪开始、按什么顺序跑。
2. **全局 Provider 装配** —— 四层「外套」分别提供什么。
3. **初始化错误监听与 db_version_too_new 分支** —— 启动期出错时的三岔路口。
4. **数据库版本过新恢复界面（DatabaseUpgrade）** —— 本次增量新增的恢复界面。
5. **App.tsx 面板组合** —— 主界面如何用状态把面板拼起来。

---

### 4.1 bootstrap 启动序列

#### 4.1.1 概念说明

「bootstrap（引导）」这个词来自「提着鞋带把自己提起来」的比喻，指程序启动时那段「先把环境准备好、再进入正式运行」的过程。

cc-switch 的前端 bootstrap 要解决一个现实问题：**后端在启动阶段可能已经发现了问题**——既可能是配置文件损坏（用户手改 `config.json` 改坏了 JSON 语法），也可能是**数据库版本比应用支持的还新**（比如用更新版本或第三方客户端创建过数据库）。如果前端不管三七二十一直接把主界面渲染出来，用户会看到一个「看起来正常但其实数据全错、甚至一操作就崩」的界面，这非常危险。

所以前端的策略是：**先问一句后端『你那边启动顺利吗？』，再根据后端的回答走三条不同的路**——可恢复就给恢复界面、不可恢复就弹窗退出、一切正常才渲染主界面。这就是 `bootstrap()` 这个 async 函数存在的意义。

#### 4.1.2 核心流程

把启动时序画成一条链（从上到下是时间顺序）：

```text
1. 浏览器/WebView 加载 src/index.html
        │  (里面有一行 <script src="./main.tsx">)
        ▼
2. 执行 src/main.tsx 顶层代码（import 全部执行一遍）
   ├─ import i18n → 触发 i18n 初始化（探测语言、加载翻译）   【副作用】
   ├─ 平台检测：给 <body> 加 is-mac 等样式 class
   └─ 注册 configLoadError 事件监听（被动接收后端报错）
        ▼
3. 调用 void bootstrap()   【main.tsx 最后一行】
        │
        ▼
4. bootstrap() 内部：
   ├─ invoke("get_init_error")  主动问后端：有错吗？
   │     │
   │     ├─ kind === "db_version_too_new"
   │     │       └─ 渲染 <DatabaseUpgrade/> 恢复界面，提前 return  ★ 本次新增（4.4）
   │     │
   │     ├─ 有 path/error
   │     │       └─ handleConfigLoadError() → 弹窗 → exit(1)  【进程退出，到此结束】
   │     │
   │     └─ 无错（null）
   │             └─ 继续往下
        ▼
5. ReactDOM.createRoot(...).render( <Provider 树> <App/> </Provider 树> )
        │
        ▼
6. App 组件挂载，主界面出现
```

关键点有三个：

- **步骤 2 的 i18n 初始化是「导入即执行」**：`import i18n from "./i18n"` 这一行不只是引入变量，它会在模块加载时立刻调用 `i18n.init()`（详见 4.2）。所以 i18n 在 bootstrap 之前就已经准备好了，这也是为什么步骤 4 的报错弹窗和恢复界面都能用 `i18n.t(...)` 显示中文。
- **步骤 4 的「先查后渲染」是 fail-fast（快速失败）设计**：宁可早退，也不带病运行。
- **步骤 4 现在是一个三岔路口**：本次增量（`edeee25f`）在原来的「弹窗退出」之外，新增了一条「渲染恢复界面」的支路，专门处理数据库版本过新这种**可恢复**的故障。

#### 4.1.3 源码精读

**起点：HTML 外壳提供挂载点。** [src/index.html:8-11](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/index.html#L8-L11) 里只有一个空 `<div id="root">` 和一行指向 `main.tsx` 的脚本标签——这就是整棵前端代码的入口。

**main.tsx 顶层的平台检测**（为不同操作系统加样式钩子）：

[src/main.tsx:19-28](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L19-L28) 通过 `navigator.userAgent` 判断是否 macOS，若是则给 `body` 加 `is-mac` class，方便 CSS 做平台差异化样式（比如 macOS 的红绿灯按钮位置）。外层套 `try/catch` 是为了在非浏览器环境（单元测试）下也不崩。

**bootstrap 函数本体**——这是启动主线的核心，本次增量在其内部插入了 `db_version_too_new` 分支：

[src/main.tsx:76-116](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L76-L116)：

```ts
async function bootstrap() {
  // 启动早期主动查询后端初始化错误，避免事件竞态
  try {
    const initError = (await invoke(
      "get_init_error",
    )) as ConfigLoadErrorPayload | null;
    if (initError && initError.kind === "db_version_too_new") {
      // 数据库版本过新：渲染应用内「升级应用」恢复界面，不进入正常 App
      ReactDOM.createRoot(document.getElementById("root")!).render(
        <React.StrictMode>
          <ThemeProvider defaultTheme="system" storageKey="cc-switch-theme">
            <DatabaseUpgrade payload={initError} />
            <Toaster />
          </ThemeProvider>
        </React.StrictMode>,
      );
      return;
    }
    if (initError && (initError.path || initError.error)) {
      await handleConfigLoadError(initError);
      return; // 注意：不会执行到这里，因为 exit(1) 会终止进程
    }
  } catch (e) {
    console.error("拉取初始化错误失败", e);
  }

  ReactDOM.createRoot(document.getElementById("root")!).render(/* ... 正常 Provider 树 + App ... */);
}

void bootstrap();
```

注意四件事：

1. **新增的 `import { DatabaseUpgrade }`** 在文件顶部 [src/main.tsx:4](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L4)，恢复界面组件就这样被引入了入口。
2. 注释「避免事件竞态」点出了设计动机：光靠监听事件不够稳（事件可能在监听器注册前就发出来了），所以还要**主动拉取一次**。详见 4.3。
3. `catch` 里只打日志、**继续往下渲染**——意思是「连查后端都失败了，那就当没出错，照常启动」，这是一种宽容降级。
4. 最后一行 [src/main.tsx:118](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L118) `void bootstrap()` 用 `void` 明确表示「我故意不 await 这个 Promise」，因为这是程序的顶层入口，没人会去等它。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一遍启动时序，确认你对顺序与分支的理解。

**操作步骤**：

1. 打开 [src/index.html](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/index.html)，找到 `<div id="root">` 和 `<script>` 标签，确认入口是 `./main.tsx`。
2. 打开 [src/main.tsx](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx)，按从上到下的顺序数一遍：第 1～16 行是 import、第 19～28 行是平台检测、第 76～116 行是 `bootstrap()`、第 118 行是 `void bootstrap()`。
3. 在脑海里（或纸上）把上面 4.1.2 的流程图抄一遍，把每一步对应的行号标上去，特别注意 `bootstrap()` 里现在有 **三个判断分支**。

**需要观察的现象**：你会注意到，整个 `main.tsx` 里真正「调用」的顶层语句只有一句——第 118 行的 `void bootstrap()`。其余全是定义（函数、interface）和 import。

**预期结果**：你能清晰地说出「i18n 初始化发生在 bootstrap 调用之前，因为它是 import 的副作用；而 bootstrap 内部会先尝试把 `db_version_too_new` 和配置错误都拦下来，最后才渲染正常 App」。

#### 4.1.5 小练习与答案

**练习 1**：如果把最后一行 `void bootstrap()` 删掉，应用会怎样？

> **答案**：`bootstrap()` 被定义了但从不调用，于是 `ReactDOM.createRoot(...).render()` 永远不会执行，界面上 `#root` 永远是空的——应用「启动了但什么都没显示」。

**练习 2**：为什么 `bootstrap()` 要写成 `async` 函数？

> **答案**：因为它内部要 `await invoke("get_init_error")` 去异步地问后端。只有 `async` 函数才能用 `await`。如果改成同步函数，就拿不到后端的错误信息了。

---

### 4.2 全局 Provider 装配

#### 4.2.1 概念说明

React 的 **Provider 模式**是一种「在组件树最外层放一个数据源，让里面所有组件都能读到」的机制。你可以把它想象成「给整栋楼装一个中央空调」——装一次，每个房间都能用。

cc-switch 在 `bootstrap()` 渲染**正常 App** 时，给 `App` 套了四层「外套」：

| 层 | Provider | 提供什么 |
|---|---|---|
| 最外 | `React.StrictMode` | 开发模式下帮开发者发现潜在问题（不是数据 Provider，是开发辅助） |
| 第 2 层 | `QueryClientProvider` | TanStack Query 的服务器状态缓存（所有 `useQuery`/`useMutation` 的底座） |
| 第 3 层 | `ThemeProvider` | 当前主题（light/dark/system），并负责把主题应用到 `<html>` 和原生窗口 |
| 第 4 层 | `UpdateProvider` | 应用更新状态，启动后自动检查新版本 |

另外，**i18n（国际化）** 虽然不是一个 JSX Provider，但它通过 `import` 副作用在更早就完成了初始化，本质上也起着「全局提供翻译能力」的作用，所以我们把它一起讲。

> 注意：这四层是「正常 App」的标配。当走到 `db_version_too_new` 分支时，恢复界面 `DatabaseUpgrade` 只套了 `ThemeProvider` + `Toaster`，没有 `QueryClientProvider` / `UpdateProvider`——原因是它不需要服务器数据缓存，更新检查也是它自己用专门命令做的（4.4 会详述）。

#### 4.2.2 核心流程

正常 App 的 Provider 嵌套顺序就是 JSX 的层层包裹顺序，从外到内依次是：

```text
<React.StrictMode>
  <QueryClientProvider client={queryClient}>
    <ThemeProvider defaultTheme="system" storageKey="cc-switch-theme">
      <UpdateProvider>
        <App />          ← 主界面
        <Toaster />      ← 全局消息提示（sonner）
      </UpdateProvider>
    </ThemeProvider>
  </QueryClientProvider>
</React.StrictMode>
```

为什么是这个顺序？因为**外层的依赖更基础**：

- `QueryClientProvider` 必须在最外（或至少在用 query 的组件之外），因为 `App` 里大量使用 `useQuery`。
- `ThemeProvider` 要在 `App` 外，因为 `App` 里的组件会根据主题渲染不同样式。
- `UpdateProvider` 放最内层，因为它依赖前两者都已就绪，且它内部会用 `useEffect` 在挂载后异步检查更新。

#### 4.2.3 源码精读

**Provider 套娃的 JSX**：[src/main.tsx:104-115](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L104-L115)，注意 `queryClient` 是从 `@/lib/query` 导入的，`<App />` 和 `<Toaster />` 一起放在 `UpdateProvider` 内部。

**第 2 层：queryClient 的全局默认配置**——[src/lib/query/queryClient.ts:3-14](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/lib/query/queryClient.ts#L3-L14)：

```ts
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,                  // 查询失败重试 1 次
      refetchOnWindowFocus: true,// 窗口重新获得焦点时自动刷新数据
      staleTime: 0,              // 数据立即视为「过期」，需要时即重新请求
    },
    mutations: {
      retry: false,              // 写操作（增删改）不自动重试，避免重复写入
    },
  },
});
```

这几行决定了全应用的数据刷新行为：`refetchOnWindowFocus: true` 解释了「为什么你切到别的窗口再切回来，列表数据会自动刷新」；`mutations.retry: false` 则是为了避免「切换供应商」这类写操作失败后自动重试导致重复写入。

> 这个 `queryClient` 还通过 [src/lib/query/index.ts](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/lib/query/index.ts) 用 `export * from "./queryClient"` 统一再导出，所以 `main.tsx` 里 `import { queryClient } from "@/lib/query"` 能直接拿到。这个文件是 query 领域的「汇总出口」，后续 u5-l2 会深入。

**第 3 层：ThemeProvider**——[src/components/theme-provider.tsx:27-31](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/theme-provider.tsx#L27-L31) 接收 `defaultTheme="system"` 和 `storageKey="cc-switch-theme"`。它的核心副作用在 [src/components/theme-provider.tsx:55-72](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/theme-provider.tsx#L55-L72)：监听 `theme` 变化，给 `document.documentElement` 切换 `light`/`dark` class（Tailwind 靠这两个 class 切换暗色模式）。当 `theme==="system"` 时，还会用 `matchMedia("(prefers-color-scheme: dark)")` 跟随系统。

特别值得一提的是 [src/components/theme-provider.tsx:98-128](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/theme-provider.tsx#L98-L128)：它还会通过 `invoke("set_window_theme", ...)` 把主题同步给**原生窗口**（Windows/macOS 的标题栏颜色），让 WebView 内外主题一致——这是 Tauri 应用才需要的细节。这也是为什么 `DatabaseUpgrade` 恢复界面也必须套一层 `ThemeProvider`：否则它在数据库异常时连主题都对不上。

**第 4 层：UpdateProvider**——[src/contexts/UpdateContext.tsx:30-31](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/contexts/UpdateContext.tsx#L30-L31) 定义 Provider。它的「启动后自动检查更新」逻辑在 [src/contexts/UpdateContext.tsx:118-126](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/contexts/UpdateContext.tsx#L118-L126)：用 `setTimeout(..., 1000)` 延迟 1 秒再检查——注释明确写「延迟1秒后检查，避免影响启动体验」，这是一个典型的「把非关键异步任务推迟到启动之后」的优化。检查结果（是否有新版本、用户是否已忽略）通过 Context 暴露给内层组件（如 `UpdateBadge`）。

**i18n 全局初始化**——[src/i18n/index.ts:79-90](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/i18n/index.ts#L79-L90) 调用 `i18n.use(initReactI18next).init({...})`。这段代码在 `main.tsx` 第 8 行 `import i18n from "./i18n"` 时**立即执行**，所以 `App` 渲染时翻译已经就绪。

语言是怎么选的？看 [src/i18n/index.ts:13-62](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/i18n/index.ts#L13-L62) 的 `getInitialLanguage()`：先读 `localStorage` 的 `language` 键；没有就读 `navigator.language`，按 `zh`/`zh-TW`/`ja`/`en` 前缀匹配；都匹配不上就用默认值 `"zh"`（第 11 行 `DEFAULT_LANGUAGE`）。这就是「第一次打开是中文、切过英文后下次还是英文」的原因。本次增量还顺带在四种语言包（`zh`/`zh-TW`/`en`/`ja`）里新增了 `dbUpgrade.*` 一组文案，供恢复界面使用。

#### 4.2.4 代码实践

**实践目标**：搞清楚每一层 Provider 各管一件事，互不串台。

**操作步骤**：

1. 打开 [src/main.tsx:104-115](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L104-L115)，把正常 App 的四层嵌套从外到内抄成一张「层级表」。
2. 对每一层，点进对应的源码文件，找到它「提供数据」的那行（比如 ThemeProvider 提供的是 `{ theme, setTheme }`，见 [theme-provider.tsx:130-139](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/theme-provider.tsx#L130-L139)）。
3. 思考：如果把 `ThemeProvider` 和 `QueryClientProvider` 的顺序对调，会出问题吗？

**需要观察的现象**：你会发现每个 Provider 内部都有一段「用 `useEffect` 在挂载后做某事」的代码（ThemeProvider 改 DOM class、UpdateProvider 延迟查更新），这是 Provider 模式的常见写法。

**预期结果**：你能口头说出「`QueryClientProvider` 管数据缓存、`ThemeProvider` 管深浅色、`UpdateProvider` 管版本检查、i18n 管语言」。

**待本地验证**（可选）：运行 `pnpm dev` 启动应用，在系统设置里切换深色/浅色模式，观察应用是否跟随变化（验证 `ThemeProvider` 的 `matchMedia` 监听）。

#### 4.2.5 小练习与答案

**练习 1**：`<App />` 和 `<Toaster />` 为什么放在 `UpdateProvider` 里面、而不是外面？

> **答案**：因为 `App` 内部可能需要通过 `useUpdate()`（[UpdateContext.tsx:144-150](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/contexts/UpdateContext.tsx#L144-L150)）读取更新状态来显示更新徽章；`Toaster` 是消息提示组件，放哪一层都能用，但跟 `App` 一起放在最内层方便统一管理。React 的规则是：**要读 Context 的组件必须在对应 Provider 内部**。

**练习 2**：i18n 没有写成 JSX Provider，为什么 `App` 里还能用 `useTranslation()`？

> **答案**：因为 i18n 用的是「模块级单例 + `initReactI18next` 插件」的方式，初始化发生在 `import` 副作用里（[i18n/index.ts:79](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/i18n/index.ts#L79)），它不依赖组件树层级，所以不需要 Provider 包裹。

---

### 4.3 初始化错误监听与 db_version_too_new 分支

#### 4.3.1 概念说明

这是本讲最值得细看的设计。前端要在启动期捕获后端的初始化错误，但它用了**两套互补的机制**：

1. **推（push）——事件监听**：前端 `listen("configLoadError", ...)` 注册一个监听器，等后端主动 `emit` 这个事件。
2. **拉（pull）——命令调用**：前端 `invoke("get_init_error")` 主动问后端「你现在有没有积压的错误？」

为什么要两套？因为存在**竞态（race condition）**：如果后端在「前端还没来得及注册监听器」之前就把错误事件发出去了，那前端就永远收不到了。于是开发者在「推」的基础上，又加了「拉」作为兜底——启动时主动查一次。代码注释 [src/main.tsx:77](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L77) 一语道破：「启动早期主动查询后端初始化错误，避免事件竞态」。

而「拉」到的错误，现在会被分成**两条性质不同的处理路径**：

- **`kind === "db_version_too_new"`：可恢复故障** → 渲染 `DatabaseUpgrade` 恢复界面，让用户自助升级（详见 4.4）。
- **有 `path`/`error`（如配置 JSON 损坏）：不可恢复故障** → 走 `handleConfigLoadError`，弹原生对话框后 `exit(1)` 强制退出，**不提供「取消」按钮**，因为配置已损坏、应用无法正常运行。

> 为什么数据库版本过新要区别对待？因为「数据库版本过新」往往能通过「升级到新版应用」来解决——它是**可恢复**的；而「配置文件语法损坏」只能靠用户手动修文件，应用自身帮不上忙。对可恢复故障给出恢复界面、对不可恢复故障直接退出，是更精细的降级策略。

#### 4.3.2 核心流程

```text
后端启动
   │
   ├─ 若启动出错 → set_init_error(payload) 把错误存进全局静态变量
   │                （init_status.rs，用 OnceLock<RwLock<...>> 保存）
   │                payload 现在带 kind/db_version/supported_version 字段
   ▼
前端 main.tsx
   │
   ├─【推】listen("configLoadError", 回调)      ← 注册被动监听
   │
   └─【拉】bootstrap() 里 invoke("get_init_error")
              │
              ├─ kind === "db_version_too_new"
              │      └─ 渲染 <DatabaseUpgrade/>，return（进程继续，等待用户操作）  ★ 新增
              │
              ├─ 有 path/error
              │      └─ handleConfigLoadError()
              │            ├─ message(...) 弹原生对话框（显示路径+错误详情）
              │            └─ exit(1)  ← 进程结束，正常 App 根本不渲染
              │
              └─ 返回 null（无错）
                     └─ 渲染正常 App
```

后端那一侧，`get_init_error` 命令只是把全局变量里的错误读出来返回，逻辑未变：

[src-tauri/src/commands/misc.rs:78-83](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/commands/misc.rs#L78-L83)

```rust
/// 获取应用启动阶段的初始化错误（若有）。
/// 用于前端在早期主动拉取，避免事件订阅竞态导致的提示缺失。
#[tauri::command]
pub async fn get_init_error() -> Result<Option<InitErrorPayload>, String> {
    Ok(crate::init_status::get_init_error())
}
```

而错误是怎么「存」进去的，以及它现在长什么样，看 [src-tauri/src/init_status.rs:21-36](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L21-L36)：用一个 `OnceLock<RwLock<Option<InitErrorPayload>>>` 全局静态变量，`set_init_error` 写入、`get_init_error` 读取。`OnceLock` 保证全局只初始化一次，`RwLock` 保证多线程读写安全——这是 Rust 里典型的「全局可变状态」写法（后端数据库连接等也会用类似模式，u2-l2 会讲）。

#### 4.3.3 源码精读

**错误数据的类型定义**——这次增量是理解整条链路的关键。

后端 [src-tauri/src/init_status.rs:4-19](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L4-L19) 给 `InitErrorPayload` 新增了三个字段：

```rust
pub struct InitErrorPayload {
    pub path: String,
    pub error: String,
    /// 错误类别。`Some("db_version_too_new")` 表示数据库版本过新（应用过旧），
    /// 前端据此展示「升级应用」恢复界面而非直接退出。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    /// 磁盘上数据库的 user_version（数据库版本过新时填充）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub db_version: Option<i32>,
    /// 当前应用支持的 SCHEMA_VERSION（数据库版本过新时填充）。
    /// 当升级到最新版后 db_version 仍 > supported_version，说明可能由第三方客户端创建。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub supported_version: Option<i32>,
}
```

要点：

- `#[serde(skip_serializing_if = "Option::is_none")]` 表示这三个字段为 `None` 时**不会序列化进 JSON**。所以「配置损坏」的旧错误对象里根本没有 `kind` 字段，前端 `initError.kind` 自然是 `undefined`，不会误命中 `db_version_too_new` 分支——这是类型设计上的安全闸。
- `db_version` / `supported_version` 专门给恢复界面显示「数据库版本 vN · 应用支持 vM」用（见 4.4）。

前端这边，对应的类型 [src/main.tsx:31-36](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L31-L36) 同步加上了 `kind?: string`：

```ts
interface ConfigLoadErrorPayload {
  path?: string;
  error?: string;
  /** "db_version_too_new" 表示数据库版本过新，渲染应用内升级恢复界面 */
  kind?: string;
}
```

> 注意：前端类型目前没显式声明 `db_version` / `supported_version`，但 `DatabaseUpgrade` 自己用了一个更宽的 props 类型把它们读出来（见 4.4），所以信息能完整传递。

**统一的错误处理函数（不可恢复路径）**——[src/main.tsx:42-64](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L42-L64)：

```ts
async function handleConfigLoadError(payload: ConfigLoadErrorPayload | null) {
  const path = payload?.path ?? "~/.cc-switch/config.json";
  const detail = payload?.error ?? "Unknown error";

  await message(
    i18n.t("errors.configLoadFailedMessage", { path, detail, /* ...默认文案... */ }),
    { title: i18n.t("errors.configLoadFailedTitle", { defaultValue: "配置加载失败" }), kind: "error" },
  );

  await exit(1);   // ← 关键：强制退出，退出码 1 表示异常
}
```

要点：

- `path` / `detail` 都有 `??` 兜底默认值，防止后端给了空对象。
- `message(...)` 来自 `@tauri-apps/plugin-dialog`，弹的是**系统原生对话框**（不是网页弹窗），因为此刻 React 还没渲染。
- `exit(1)` 来自 `@tauri-apps/plugin-process`，`1` 是非零退出码，表示「异常退出」。
- 注意：`exit(1)` 之后那行 `return` 实际上**永远执行不到**（进程已被终止），注释也写明了这点。

**推：事件监听的注册**——[src/main.tsx:66-74](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L66-L74)：

```ts
try {
  void listen("configLoadError", async (evt) => {
    await handleConfigLoadError(evt.payload as ConfigLoadErrorPayload | null);
  });
} catch (e) {
  console.error("订阅 configLoadError 事件失败", e);
}
```

这段在 `bootstrap()` **之前**就执行了（它就在模块顶层）。`listen` 返回一个 Promise（异步注册），这里用 `void` 表示不等待注册完成就继续往下走——这也是潜在竞态的来源之一，所以才需要「拉」来兜底。外层 `try/catch` 是为了在非 Tauri 环境（如纯浏览器调试、单元测试）下订阅失败也不崩。

**拉：bootstrap 里的三岔判断**——核心就是 [src/main.tsx:82-98](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L82-L98)。两个 `if` 的顺序很关键：**先判 `kind`，再判 `path`/`error`**。因为一个数据库版本过新的 payload 也可能带着 `error` 描述，必须先用更具体的 `kind` 把它截走，否则会被后面的「弹窗退出」分支误伤。

#### 4.3.4 代码实践（本讲主实践任务之一）

**实践目标**：把「后端报错 → 前端如何响应」这条链路彻底走通，并对比两种错误分支。

**操作步骤**：

1. 在 [src/main.tsx](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx) 中定位三处错误相关代码：
   - 「推」的监听：第 [66-74 行](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L66-L74) 的 `listen("configLoadError", ...)`。
   - 「拉」的查询：第 [79-81 行](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L79-L81) 的 `invoke("get_init_error")`。
   - 两个判断分支：`db_version_too_new` 在第 [82-93 行](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L82-L93)、普通 `path/error` 在第 [94-98 行](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L94-L98)。
2. 在 [src-tauri/src/commands/misc.rs:78-83](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/commands/misc.rs#L78-L83) 确认这个命令只是把后端全局变量里的错误读出来返回。
3. 在 [src-tauri/src/init_status.rs:4-19](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src-tauri/src/init_status.rs#L4-L19) 确认 `kind`/`db_version`/`supported_version` 三个新字段的语义，以及 `skip_serializing_if` 如何保证旧错误对象不带 `kind`。

**需要观察的现象 / 你要回答的问题**：

> 当后端 `get_init_error` 返回一个错误时，前端会发生什么？`db_version_too_new` 和普通配置错误分别走哪条路？

**预期答案（对比表）**：

| 维度 | `db_version_too_new` 分支（[L82-93](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L82-L93)） | 普通 `path/error` 分支 → `handleConfigLoadError`（[L94-98 / 42-64](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L42-L64)） |
|---|---|---|
| 触发条件 | `initError.kind === "db_version_too_new"` | `initError.path` 或 `initError.error` 有值 |
| 根本原因 | 数据库由更新/第三方版本创建，当前应用 schema 太旧 | 配置文件 JSON 损坏等 |
| 是否可恢复 | 可恢复（升级应用即可） | 不可恢复（需用户手动修文件） |
| 前端动作 | 渲染 `<DatabaseUpgrade/>` 恢复界面后 `return`（**进程继续**） | 弹原生对话框 → `exit(1)`（**进程退出**） |
| Provider 套娃 | 仅 `ThemeProvider` + `Toaster` | 无（对话框模式，React 树不渲染） |
| 用户能否自救 | 能（一键升级 / 打开发布页 / 打开配置目录 / 退出） | 不能，只能退出后手动修复或从备份恢复 |

具体到普通配置错误路径：`bootstrap()` 判定为真 → 调 `handleConfigLoadError` → 弹出**原生错误对话框**（标题「配置加载失败」，正文显示路径与详情，提示「应用将退出以便您进行修复」）→ 关闭后 `exit(1)` → `ReactDOM.createRoot(...).render()` **永远不会执行**，用户根本看不到主界面。这是一种 **fail-fast** 策略。

**待本地验证**（可选，需改动配置文件）：备份后人为破坏 `~/.cc-switch/config.json` 的 JSON 语法，运行 `pnpm dev`，观察是否弹出该对话框并退出。**注意：操作前务必备份原文件。** 数据库版本过新场景较难手工构造，可阅读 `DatabaseUpgrade` 的相关测试或暂标「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：既然有了「拉」（`get_init_error`），为什么还要保留「推」（`listen`）？

> **答案**：两者时机不同。「拉」只在 `bootstrap()` 启动那一刻查一次；如果配置错误是在应用运行过程中（比如某次同步后）才产生的，就需要「推」来实时通知。它们是互补关系，覆盖「启动期积压错误」和「运行期新增错误」两种场景。

**练习 2**：`bootstrap()` 里为什么要把 `kind === "db_version_too_new"` 的判断放在 `path || error` 的判断**之前**？

> **答案**：因为数据库版本过新的 payload 通常也带一段 `error` 描述（说明版本不兼容）。如果先判 `path || error`，这条可恢复错误就会被 `handleConfigLoadError` 当成普通配置损坏，直接 `exit(1)`，用户就失去了「升级应用」的自救机会。先按更具体的 `kind` 截走，才能让它进入恢复界面分支。

**练习 3**：`handleConfigLoadError` 为什么不给用户「取消」选项，而是直接 `exit(1)`？

> **答案**：因为配置文件已损坏，应用在这种状态下无法正常读写配置，强行渲染只会让用户面对一个数据错乱的界面，甚至可能把损坏的配置再次写坏。直接退出、引导用户修文件或从备份恢复，是最安全的做法（代码注释 [main.tsx:38-41](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L38-L41) 也说明了这一点）。

---

### 4.4 数据库版本过新恢复界面（DatabaseUpgrade）

> 这是本次增量（`edeee25f`）新增的最小模块。它对应 PR #4575「in-app recovery screen with upgrade button when DB version is too new」。

#### 4.4.1 概念说明

当后端在启动预检时发现「磁盘上的数据库 `user_version` 高于当前应用支持的 `SCHEMA_VERSION`」，就会把 `kind` 设成 `"db_version_too_new"` 并附带两个版本号。前端拿到后**不退出**，而是渲染 [src/components/DatabaseUpgrade.tsx](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx) 这个**应用内恢复界面**。

它要解决的问题是：这种情况通常是「用户装过更新版本、或用过第三方兼容客户端」造成的，而**最可能的解决办法就是升级应用**。所以与其像配置损坏那样直接退出把用户晾在一边，不如给一个能自助操作的界面：

- 启动时自动检查「是否有可用更新」：
  - **有更新** → 显示「升级应用」按钮，一键下载 + 安装 + 重启，并展示下载进度条。
  - **没有更新**（已是最新但数据库仍过新）→ 说明升级也救不了（多半是第三方客户端造的库），改成红色「升级也无法解决」提示，引导用户备份、改用兼容客户端或去发布页求助。

这个组件有两个值得学习的设计点：① 用一个 `phase` 状态变量把界面切成几个互斥阶段，是一个教科书式的**前端状态机**；② 它故意只套了 `ThemeProvider`，不复用正常 App 的 `QueryClientProvider`/`UpdateProvider`，因为它的更新检查是直接调命令、不走 TanStack Query。

#### 4.4.2 核心流程

`phase` 是一个只能在 5 个取值间切换的状态机：

```text
                       ┌──────────────┐
            挂载即进入 │  checking     │  调 invoke("check_app_update_available")
                       └──────┬───────┘
                              │ 返回 version / null
              ┌───────────────┼───────────────────────┐
        有更新 │                                       │ 无更新（已是最新）
              ▼                                       ▼
        ┌───────────┐                           ┌───────────────┐
        │ upgradable│                           │ incompatible  │  红色「升级也无效」
        └─────┬─────┘                           └───────┬───────┘
              │ 点「升级应用」                            │ 只能打开发布页/配置目录/退出
              ▼
        ┌───────────┐  成功 → 后端下载+安装+重启（进程退出）
        │ updating  │  返回 false（竞态：更新已消失）→ incompatible
        └─────┬─────┘  抛错 → error
              │
              ▼
        ┌───────────┐  点「重试升级」→ 回到 updating
        │   error   │
        └───────────┘
```

用集合与迁移关系可以形式化为：

\[
\text{Phase} = \{\,\text{checking},\ \text{upgradable},\ \text{incompatible},\ \text{updating},\ \text{error}\,\}
\]

\[
\text{checking} \xrightarrow{\text{有更新}} \text{upgradable},\quad
\text{checking} \xrightarrow{\text{无更新}} \text{incompatible},\quad
\text{upgradable} \xrightarrow{\text{点升级}} \text{updating}
\]

\[
\text{updating} \xrightarrow{\text{失败}} \text{error},\quad
\text{updating} \xrightarrow{\text{无更新(竞态)}} \text{incompatible},\quad
\text{error} \xrightarrow{\text{重试}} \text{updating}
\]

`incompatible` 是一个**吸收态**（除了重启不会再变），`updating` 成功后进程会被后端直接重启，所以界面停留到退出即可。

#### 4.4.3 源码精读

**Props 与 phase 定义**——[src/components/DatabaseUpgrade.tsx:19-34](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L19-L34)。`DatabaseUpgradeProps.payload` 是个比前端 `ConfigLoadErrorPayload` 更宽的类型，额外带 `db_version` / `supported_version`。`Phase` 的每个取值都有注释，建议直接读这几行注释，它就是上面状态机图的文字版。

**挂载即检查更新（checking → upgradable/incompatible）**——[src/components/DatabaseUpgrade.tsx:61-83](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L61-L83)：

```ts
useEffect(() => {
  let cancelled = false;
  (async () => {
    try {
      const version = await invoke<string | null>("check_app_update_available");
      if (cancelled) return;
      if (version) { setAvailableVersion(version); setPhase("upgradable"); }
      else { setPhase("incompatible"); }
    } catch {
      // 检查失败（如离线）：仍允许尝试升级，避免完全卡死
      if (!cancelled) setPhase("upgradable");
    }
  })();
  return () => { cancelled = true; };
}, []);
```

注意两个细节：① `check_app_update_available` 返回 `string | null`——有版本号说明有更新、`null` 说明已是最新；② 检查失败（如断网）时**故意降级成 `upgradable`** 而不是 `incompatible`，注释写明「避免完全卡死」——宁可让用户点一下升级试试，也不要因为网络问题就把人锁死。

**升级动作（upgradable → updating → 成功/失败）**——[src/components/DatabaseUpgrade.tsx:91-116](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L91-L116)：

```ts
const startUpgrade = useCallback(async () => {
  setPhase("updating");
  // ...清理旧监听...
  unlistenRef.current = await listen<DownloadProgress>(
    "update-download-progress", (e) => setProgress(e.payload));
  const updating = await invoke<boolean>("install_update_and_restart");
  // updating === true：后端会下载+安装+重启，不会返回
  // updating === false：竞态——检查时有更新、安装时已无 → 按不兼容处理
  if (!updating) setPhase("incompatible");
}, []);
```

这里用 `listen("update-download-progress", ...)` 订阅后端推送的下载进度，驱动界面上的进度条（[L211-241](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L211-L241)）。`install_update_and_restart` 返回 `boolean`：`true` 表示即将重启（函数不会真正返回到这一步之后）、`false` 表示「竞态：刚检查到有更新、点安装时却没了」，此时退回 `incompatible`。`unlistenRef` 用 `useRef` 保存取消订阅函数，确保组件卸载或重试时不会泄漏监听器。

**底部按钮区**——[src/components/DatabaseUpgrade.tsx:249-297](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L249-L297) 根据 `phase` 条件渲染不同按钮：

| phase | 显示的按钮 |
|---|---|
| `upgradable` / `error` | 「升级应用」/「重试升级」（调 `install_update_and_restart`） |
| `incompatible` / `error` | 「打开发布页」（`invoke("open_external", { url: RELEASES_URL })`） |
| 任意（非 updating） | 「打开配置目录」（`invoke("open_app_config_folder")`，方便用户备份数据库） |
| 任意（非 updating） | 「退出」（`exit(0)`，注意是 `0` 正常退出，与配置错误的 `exit(1)` 不同） |

> 其中 `RELEASES_URL` 是 [L17](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L17) 写死的 GitHub Releases 地址。

**最外层的套娃**回到 [src/main.tsx:84-92](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L84-L92)：恢复界面只包了 `ThemeProvider` + `<DatabaseUpgrade/>` + `<Toaster/>`，**没有** `QueryClientProvider`/`UpdateProvider`。这印证了 4.4.1 的判断——它自给自足，不依赖正常 App 的基础设施。

#### 4.4.4 代码实践

**实践目标**：把 `DatabaseUpgrade` 的状态机走一遍，验证你对阶段迁移的理解。

**操作步骤**：

1. 打开 [src/components/DatabaseUpgrade.tsx:29-34](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L29-L34)，把 `Phase` 的 5 个取值抄下来。
2. 对照 [L61-83](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L61-L83)（检查更新）和 [L91-116](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L91-L116)（执行升级），在纸上画出 4.4.2 的状态机图，标注每条迁移是由哪一行触发的。
3. 找到按钮区 [L249-297](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L249-L297)，确认「退出」按钮调的是 `exit(0)`（正常退出），与配置错误的 `exit(1)` 不同。

**需要观察的现象**：你会看到 `incompatible` 阶段用的是红色配色（`bg-red-*`）、其余阶段用琥珀色（`bg-amber-*`），这套配色由 [L124-134](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/components/DatabaseUpgrade.tsx#L124-L134) 的 `accent` 变量根据 `phase` 切换——这是「用状态驱动样式」的常见写法。

**预期结果**：你能说出「进入界面先是 checking，有更新就 upgradable、没有就 incompatible；点升级进入 updating，失败转 error 可重试，成功则由后端重启」。

**待本地验证**：数据库版本过新状态难以手工触发，可暂标「待本地验证」；如需观察界面，可在本地开发时临时把后端预检相关逻辑短路以注入一个 `db_version_too_new` 的 payload（仅本地调试，勿提交）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `DatabaseUpgrade` 检查更新失败（断网）时要降级成 `upgradable` 而不是 `incompatible`？

> **答案**：因为检查失败只代表「不知道有没有更新」，并不代表「确定没有更新」。如果降级成 `incompatible`（升级也无效），用户就被锁死、连试一下升级的机会都没有。降级成 `upgradable` 至少保留了「点升级试一试」的出路，是最安全的兜底。

**练习 2**：`install_update_and_restart` 返回 `false` 时为什么要转成 `incompatible`？

> **答案**：返回 `false` 意味着「点安装的那一刻已经没有可用更新了」（检查与安装之间存在竞态）。既然最新版都救不了，那这个数据库确实高于本应用支持版本，等价于 `incompatible` 场景，所以复用同一条「升级也无法解决」的提示路径。

---

### 4.5 App.tsx 面板组合

#### 4.5.1 概念说明

当 `bootstrap()` 顺利通过所有错误检查，`<App />` 终于挂载，主界面出现。`App.tsx` 是整个前端的**状态中枢**：它不画具体功能，而是负责「我现在该显示哪个面板」「用户切换工具/视图时状态怎么变」。

这里有两个核心状态：

- `activeApp`：当前选中的是哪种 CLI 工具（claude / codex / gemini / …，共 7 种）。
- `currentView`：当前显示哪个功能面板（供应商列表 / 设置 / MCP / Skills / 会话 / …）。

`App` 用一个巨大的 `switch` 语句（`renderContent` 函数）把 `currentView` 映射到对应的面板组件。这是最朴素的「视图路由」——没有引入 React Router，因为桌面应用不需要 URL，用一个状态变量就够了。

#### 4.5.2 核心流程

```text
App 组件挂载
   │
   ├─ 读初始状态：
   │    activeApp   = getInitialApp()    ← 从 localStorage 读「上次选的工具」，默认 claude
   │    currentView = getInitialView()   ← 从 localStorage 读「上次的视图」，默认 providers
   │
   ├─ 拉取数据：useProvidersQuery(activeApp)、useSettingsQuery() 等
   │
   ├─ 渲染 JSX 结构：
   │    <div 根容器>
   │      <窗口拖拽条 / 窗口控制按钮>     ← 平台相关
   │      <EnvWarningBanner/>            ← 环境变量冲突警告（条件渲染）
   │      <header>                       ← 顶部栏：标题 + 工具切换 + 功能入口按钮
   │      <main>
   │        { renderContent() }          ← ★ 核心：根据 currentView 切换面板
   │      </main>
   │      <各种 Dialog 弹窗>             ← AddProviderDialog / EditProviderDialog / ConfirmDialog...
   │    </div>
   │
   └─ 用户点击工具栏按钮 → setCurrentView("mcp") 等 → currentView 变化 → renderContent 重算 → 面板切换
```

`renderContent()` 内部就是一个 `switch (currentView)`：每个 `case` 返回一个面板组件。比如 `case "mcp"` 返回 `<UnifiedMcpPanel/>`、`case "settings"` 返回 `<SettingsPage/>`、`default`（即 `providers`）返回带动画的 `<ProviderList/>`。

#### 4.5.3 源码精读

**面板组件的批量导入**：[src/App.tsx:59-95](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L59-L95) 集中导入了几十个功能组件，是理解「App 由哪些面板拼成」的目录。这是本讲实践任务的素材库。

**View 类型定义**——[src/App.tsx:97-111](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L97-L111)，把所有合法视图列成一个字符串字面量联合类型：

```ts
type View =
  | "providers" | "settings" | "prompts" | "skills" | "skillsDiscovery"
  | "mcp" | "agents" | "universal" | "sessions" | "workspace"
  | "openclawEnv" | "openclawTools" | "openclawAgents" | "hermesMemory";
```

这意味着 `currentView` 只能取这些值之一，TypeScript 会帮你检查拼写错误——这是「类型安全」的好处。

**App 组件的开头**：[src/App.tsx:167-174](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L167-L174)，用 `useTranslation()` 拿翻译、`useQueryClient()` 拿缓存客户端，并用 `useState(getInitialApp)` / `useState(getInitialView)` 初始化两个核心状态。`getInitialApp`（[App.tsx:133-139](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L133-L139)）从 `localStorage` 读上次的选择，没有就默认 `"claude"`——这就是「关掉再打开，还是停在上次选的工具」的原因。

**核心：renderContent 视图路由**——[src/App.tsx:870-1029](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L870-L1029)，节选关键骨架（非逐字，仅示意结构）：

```tsx
const renderContent = () => {
  const content = (() => {
    switch (currentView) {
      case "settings":   return <SettingsPage ... />;
      case "prompts":    return <PromptPanel ... />;
      case "skills":     return <UnifiedSkillsPanel ... />;
      case "mcp":        return <UnifiedMcpPanel ... />;
      case "sessions":   return <SessionManagerPage ... />;
      // ... 其余 case ...
      default:           return <ProviderList ... />;  // ← providers 视图
    }
  })();
  return <AnimatePresence><motion.div key={currentView}>{content}</motion.div></AnimatePresence>;
};
```

> 说明：上面是「示例代码」，仅展示 `switch` 结构，省略了大量 props。真实代码请看 [App.tsx:870-1029](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L870-L1029)。注意外层用 `framer-motion` 的 `AnimatePresence` + `motion.div`（`key={currentView}`）实现了**切换面板时的淡入淡出动画**——key 变化会触发退出/进入动画。

**最终的 JSX 结构**：[src/App.tsx:1031-1624](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L1031-L1624) 是 `App` 的 `return`，结构是「窗口拖拽条 + 顶部 header（[L1111-L1533](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L1111-L1533)）+ `<main>{renderContent()}</main>`（[L1535-L1540](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L1535-L1540)）+ 一堆 Dialog」。顶部 header 里那一排图标按钮（Skills/Prompts/Sessions/MCP 等）点击后都是调用 `setCurrentView(...)` 来切换面板。

#### 4.5.4 代码实践（本讲主实践任务之二）

**实践目标**：看清 `App` 到底由哪些功能面板拼成。

**操作步骤**：

1. 打开 [src/App.tsx:59-95](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L59-L95)，这里是组件导入区。
2. 列出至少 **5 个**被导入的功能面板组件，并对照 [renderContent（L870-1029）](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L870-L1029) 找出每个对应哪个 `View`。

**预期结果**（参考答案，至少列出 5 个）：

| 组件 | 导入位置 | 对应 View | 作用 |
|---|---|---|---|
| `ProviderList` | [L60](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L60) | `providers`（default） | 供应商列表，主视图 |
| `SettingsPage` | [L64](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L64) | `settings` | 设置页 |
| `UnifiedMcpPanel` | [L71](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L71) | `mcp` | 统一 MCP 管理面板 |
| `PromptPanel` | [L72](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L72) | `prompts` | Prompts 管理 |
| `UnifiedSkillsPanel` | [L78](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L78) | `skills` | Skills 管理 |
| `SessionManagerPage` | [L85](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L85) | `sessions` | 会话管理器 |
| `AgentsPanel` | [L81](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L81) | `agents` | Agents 管理 |
| `AddProviderDialog` / `EditProviderDialog` | [L61-62](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/App.tsx#L61-L62) | （弹窗，非 View） | 新增/编辑供应商对话框 |

**需要观察的现象**：你会看到 `App.tsx` 顶部那一长串 import 几乎「一一对应」后续单元要讲的功能域（providers / mcp / skills / prompts / sessions / proxy / settings…），这正是 u1-l3 讲过的「按领域组织」在前端的体现。

**预期结果**：你能说出「点顶部某个图标 → 触发 `setCurrentView` → `renderContent` 的 switch 命中对应 case → 渲染对应面板」这条链路。

#### 4.5.5 小练习与答案

**练习 1**：`App` 没有用 React Router，靠什么实现「页面切换」？

> **答案**：靠一个 `currentView` 状态变量 + `renderContent()` 里的 `switch`。点击按钮调用 `setCurrentView("mcp")` 改变状态，React 重新渲染，`switch` 命中新的 `case`，返回新面板。桌面应用不需要 URL，这种「状态驱动视图」比路由库更轻。

**练习 2**：为什么 `renderContent` 外层要套 `<motion.div key={currentView}>`？

> **答案**：`key` 变化会让 React 把它当成「新组件」，配合 `framer-motion` 的 `AnimatePresence` 就能在视图切换时播放退出/进入动画（淡入淡出），让界面切换更顺滑。如果不加 `key`，React 会就地复用同一个 DOM 节点，动画就不会触发。

---

## 5. 综合实践

把本讲五个模块串起来，完成下面这个「启动链路追踪」小任务：

**任务**：假设你是一位新加入的贡献者，被分配去排查「用户反馈应用启动后白屏 / 闪退」的 bug。请结合本讲所学，画出从前端启动到界面出现的**完整检查清单**，并指出最可能出问题的 4 个环节（含本次新增的恢复界面路径）。

**参考思路**：

1. **入口是否被加载**：检查 [src/index.html](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/index.html) 的 `<script src="./main.tsx">` 是否正确，Vite 是否编译报错（`pnpm dev` 终端有无错误）。
2. **bootstrap 是否被调用**：确认 [main.tsx:118](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L118) 的 `void bootstrap()` 没被误删。
3. **是否被配置错误拦下（exit）**：后端 `get_init_error` 返回 `path/error`，`handleConfigLoadError` 弹窗后 `exit(1)`。但若是「静默白屏」（连弹窗都没有），要怀疑 `exit(1)` 之前的 `message()` 在某平台失败，或后端命令本身抛异常被 `catch` 吞掉（[main.tsx:99-102](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L99-L102)）。
4. **是否进了数据库恢复界面（本次新增）**：如果用户看到的是「数据库版本过新」界面而不是主界面，说明 `get_init_error` 返回了 `kind === "db_version_too_new"`（[main.tsx:82-93](https://github.com/farion1231/cc-switch/blob/edeee25faef80530e65529c883c90e186fcc2c6c/src/main.tsx#L82-L93)），此时 `DatabaseUpgrade` 的 `check_app_update_available` 若也失败、又被 `catch` 降级，要确认是否卡在某个 phase。
5. **Provider 套娃是否报错**：若 `ThemeProvider` / `QueryClientProvider` 内部抛错（如 localStorage 被禁用），整棵树可能白屏。可在每个 Provider 外层临时加 `ErrorBoundary` 定位。
6. **App 是否渲染**：确认 `App.tsx` 的 `renderContent()` 没有在某个 `case` 里返回 `null` 或抛错。

**最可能的 4 个环节**：① 后端配置错误导致 `exit(1)`（4.3）；② 数据库版本过新进入恢复界面但恢复流程异常（4.3 / 4.4）；③ `bootstrap()` 未被调用或 render 抛异常（4.1）；④ 某 Provider 初始化失败（4.2）。

> 这个练习不需要你真的改代码，重点是训练你「从入口顺藤摸瓜」的排查思路——这也是后续阅读任何模块的通用方法。

---

## 6. 本讲小结

- cc-switch 前端的启动主线是：**`index.html` → `main.tsx` 顶层（平台检测 + i18n 副作用 + 错误监听注册）→ `bootstrap()` → 通过错误检查后 `ReactDOM.render` 挂载 `<App/>`**。
- `bootstrap()` 现在是一个**三岔路口**：先 `invoke("get_init_error")` 问后端，按返回结果分流——`db_version_too_new` 渲染恢复界面（4.4）、`path/error` 弹窗并 `exit(1)`、`null` 才渲染正常 App。
- 初始化错误用了 **「推（`listen` 事件）+ 拉（`get_init_error` 命令）」双保险**，拉的引入正是为了「避免事件竞态」；后端 `InitErrorPayload` 本次新增的 `kind`/`db_version`/`supported_version` 字段，是区分「可恢复 vs 不可恢复」的关键。
- **本次新增的 `DatabaseUpgrade` 恢复界面**：当数据库版本过新（可恢复故障）时，不再硬退出，而是渲染一个以 `phase` 状态机驱动的自助升级界面（checking → upgradable/incompatible → updating → error），只套 `ThemeProvider`、自给自足完成更新检查与安装。
- 全局数据通过四层 **Provider** 提供：`QueryClientProvider`（数据缓存）、`ThemeProvider`（深浅色 + 同步原生窗口）、`UpdateProvider`（启动后延迟 1 秒自动检查更新），加上导入即初始化的 **i18n**（语言探测）。
- `queryClient` 的默认配置（`refetchOnWindowFocus: true`、`mutations.retry: false`）决定了全应用的刷新与重试行为。
- `App.tsx` 是**状态中枢**：用 `activeApp`（7 种工具）+ `currentView`（14 种视图）两个状态，靠 `renderContent()` 的 `switch` 实现轻量视图路由，把几十个按领域划分的功能面板拼成主界面。

---

## 7. 下一步学习建议

本讲只把前端「怎么启动、怎么拼界面、出错怎么降级」讲清楚了，但还没深入「**前端怎么和后端通信**」。建议下一步：

1. **优先读 u5-l1「类型安全的 Tauri IPC API 层」**：本讲里反复出现的 `invoke("get_init_error")`、`invoke("check_app_update_available")`、`listen("configLoadError")`、`listen("update-download-progress")` 就是 Tauri IPC。u5-l1 会讲 `src/lib/api/` 如何把这些裸调用封装成类型安全、按领域组织的 API 对象——这是理解 `App.tsx` 和 `DatabaseUpgrade` 里所有 `invoke` 的钥匙。
2. **接着读 u5-l2「TanStack Query 缓存与变更」**：本讲 4.2 提到的 `queryClient`、`useQuery`/`useMutation` 在这里深入，你会明白 `App` 里 `useProvidersQuery`、`useSettingsQuery` 是怎么把后端数据「缓存 + 自动刷新」的。
3. **若对后端启动更感兴趣**：可先跳到 u1-l5「后端启动与 Tauri 应用装配」，看 `get_init_error` 和 `db_version_too_new` 背后那段 Rust `setup` 钩子是如何做数据库版本预检、如何在版本过新时设置 `InitErrorPayload` 并跳过 DDL 写入的——和本讲 4.3 / 4.4 正好首尾呼应。
4. **动手延伸**：试着在 `bootstrap()` 的 `invoke` 调用前后各加一行 `console.log`（仅本地调试，勿提交），运行 `pnpm dev`，在开发者工具 Console 里观察日志出现的顺序，验证你对启动时序的理解。
