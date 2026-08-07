# 面板前端（React）

## 1. 本讲目标

u13-l1 讲了面板的 Go 后端：它直接读写 `config.yaml`、合成 `SystemStatus`、探活容器。本讲把视角移到这套后端的对端——React 前端 `dashboard/frontend/`，回答四个问题：

1. 一个浏览器请求进来，前端是怎么被「路由壳」组织起来的？
2. 未登录、首次安装、只读模式三类「门控」分别在哪里拦截？
3. 概览页（Dashboard）从哪些接口取数、又如何把配置与运行时状态塑造成可视化统计？
4. 配置页（Config）如何按段渲染、如何把用户编辑写回后端、又如何与只读/权限协作？

学完后你应当能：在源码里定位任意一个页面在路由表中的位置、说明它经过哪几道门控、指出它消费哪些 `/api/*` 端点，并能动手加一个导航项或改一段统计逻辑。

## 2. 前置知识

- **React 函数组件与 Hooks**：`useState`/`useEffect`/`useMemo`/`useCallback`。前端用 React 18。
- **React Router v6**：本讲的「路由壳」大量使用 v6 的「嵌套布局路由 + `<Outlet />`」写法——一个 `<Route element={<X/>}>` 不带 `path`，仅作为布局壳，子路由渲染到它的 `<Outlet />` 里。
- **React.lazy + Suspense**：按需加载（代码分割），首屏只加载必要 chunk。
- **Context**：跨层共享全局状态（鉴权、设置、安装状态）。
- 承接 u13-l1：前端消费的后端端点（`/api/router/config/all`、`/api/router/config/update`、`/api/status`、`/api/settings` 等）由上一讲的 Go handler 提供；前端与后端是两个独立进程，靠 HTTP 薄交互。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `dashboard/frontend/src/App.tsx` | 应用根组件：iframe 自检 + 三层 Context Provider 包裹。 |
| `dashboard/frontend/src/app/AppRouter.tsx` | 路由壳：安装态加载门 + 公共路由 + 受保护路由嵌套。 |
| `dashboard/frontend/src/app/AuthGate.tsx` | 鉴权门：未登录重定向到 `/login` 并记 `from`。 |
| `dashboard/frontend/src/app/AuthenticatedShell.tsx` | 安装态门：setup 模式强制跳 `/setup`。 |
| `dashboard/frontend/src/app/routeManifest.ts` | 声明式路由表（shell 路由 + 重定向 + 兜底）。 |
| `dashboard/frontend/src/app/routeLoaders.ts` | 懒加载 chunk 入口 + 悬停预加载。 |
| `dashboard/frontend/src/app/RecoverableLazyRoute.tsx` | 可恢复懒加载组件：Suspense + ErrorBoundary + 重试。 |
| `dashboard/frontend/src/contexts/{Auth,Readonly,Setup}Context.tsx` | 三类全局门控状态。 |
| `dashboard/frontend/src/utils/accessControl.ts` | 基于权限/角色的路径级访问控制。 |
| `dashboard/frontend/src/pages/DashboardPage.tsx` | 概览页：配置 + 状态取数、轮询、统计塑造。 |
| `dashboard/frontend/src/pages/ConfigPage.tsx` | 配置页：分段渲染 + 读/写/规范化。 |
| `dashboard/frontend/src/components/ConfigNav.tsx` | 配置页八段导航定义（`ConfigSection` 类型来源）。 |

## 4. 核心概念与源码讲解

### 4.1 路由壳：从 App.tsx 到嵌套路由

#### 4.1.1 概念说明

「路由壳」指页面真正渲染前的那层骨架：它决定整棵组件树的 Provider 包裹顺序、决定哪些 URL 是公共的（登录页、落地页）、哪些必须先过门控，以及每个页面用哪个代码块（chunk）懒加载。Semantic Router 前端的路由壳遵循一个清晰的分层：

> `App（Provider 桶）` → `AppRouter（BrowserRouter + 安装态门）` → `公共路由 / AuthGate 门 / AuthenticatedShell 布局壳` → 各懒加载页面。

把 Provider、门控、布局壳三者拆成独立组件，使「谁能进」「进来穿什么外壳」「进来干什么」三件事互不耦合。

#### 4.1.2 核心流程

1. `App` 挂载时做两件事：设置暗色主题属性；检测自身是否被嵌在 iframe 里（防止 dashboard 把自己嵌进自己形成循环），若是则渲染警告页直接返回。
2. 否则按 **AuthProvider → ReadonlyProvider → SetupProvider → AppRouter** 的顺序包裹。这个顺序不是任意的：`ReadonlyProvider` 内部要 `useAuth()` 取 token，所以 Auth 必须在外；`SetupProvider` 最内。
3. `AppRouter` 先读 `useSetup()`：若 `isLoading` 或 `error`，整屏渲染 `SetupStatusPage`（loading/error 两种变体），此时连路由表都不挂载。
4. setup 态就绪后挂载 `BrowserRouter`，定义三类路由：
   - 公共：`/`、`/login`、`/auth/transition`；
   - 受保护：一个 `<Route element={<AuthGate/>}>`（无 `path`，纯门控），其内嵌 `<Route element={<AuthenticatedShell/>}>`（布局壳），再内嵌 `renderAuthenticatedAppRoutes(...)` 返回的全部业务路由；
   - 兜底：`*` 重定向。
5. 每个业务路由的元素都是 `<RecoverableLazyRoute loader={...}>`，按需加载对应页面 chunk。

#### 4.1.3 源码精读

`App.tsx` 的 Provider 桶——三层 Context 的包裹顺序就是依赖顺序：

[dashboard/frontend/src/App.tsx#L69-L77](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/App.tsx#L69-L77) —— `AuthProvider` 最外、`SetupProvider` 最内、`AppRouter` 在最里层渲染。

iframe 自检发生在挂载副作用里，命中即短路渲染警告页，根本不进入 Provider 桶：

[dashboard/frontend/src/App.tsx#L10-L19](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/App.tsx#L10-L19) —— `window.self !== window.top` 判定嵌套，配合「检查 Grafana 路径与后端代理配置」的提示，说明这套面板可能被嵌入监控大盘。

`AppRouter` 的路由壳核心——先做 setup 态门，再挂 `BrowserRouter` 并组织公共/受保护/兜底三类路由：

[dashboard/frontend/src/app/AppRouter.tsx#L48-L75](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/app/AppRouter.tsx#L48-L75) —— 注意 `<Route element={<AuthGate/>}>` 与 `<Route element={<AuthenticatedShell/>}>` 都没有 `path`，是 v6 的「路径less 布局路由」，子路由通过 `<Outlet/>` 渲染。

业务路由表是**声明式数据**驱动的，不是散落的 `<Route>`：`shellRouteDefinitions` 一个数组定义了所有走 shell 布局的页面：

[dashboard/frontend/src/app/routeManifest.ts#L33-L57](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/app/routeManifest.ts#L33-L57) —— 每条带 `page` 字段，`AuthenticatedAppRoutes` 据此把 `page` 映射到对应懒加载元素（见 `shellPageElements`）。

每个页面都用同一个可恢复懒加载组件包裹，统一了 Suspense fallback 与加载失败重试：

[dashboard/frontend/src/app/RecoverableLazyRoute.tsx#L105-L127](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/app/RecoverableLazyRoute.tsx#L105-L127) —— 内部用 `ErrorBoundary` + `Suspense`，重试时 `resetDashboardRouteLoader` 清掉缓存的失败 Promise 再 `setAttempt` 强制重建 `lazy()`，避免一次 chunk 加载失败就永久卡死。

还有一个体验优化：路由表外有一份「路径匹配 → loader」的列表，供**悬停/聚焦预加载**用：

[dashboard/frontend/src/app/routeLoaders.ts#L74-L89](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/app/routeLoaders.ts#L74-L89) —— `preloadDashboardRoute` 用 `Map` 缓存预加载 Promise，且 `.catch(() => undefined)` 把「意图性预取」的失败吞掉，不变成未处理拒绝。

#### 4.1.4 代码实践

1. **实践目标**：在源码里追踪一个 URL 到它最终渲染的页面 chunk。
2. **操作步骤**：
   - 打开 `routeManifest.ts`，找到 `/evaluation` 对应的 `page` 值（应为 `'evaluation'`）。
   - 打开 `AuthenticatedAppRoutes.tsx`，在 `shellPageElements` 里用这个 `page` 找到它的 `<RecoverableLazyRoute loader={loadEvaluationPage ...}>`。
   - 打开 `routeLoaders.ts`，找到 `loadEvaluationPage = () => import('../pages/EvaluationPage')`，确认 chunk 来源。
3. **需要观察的现象**：从「声明路径」到「实际页面文件」是一条纯数据驱动的链路，中间没有任何 switch/if 分支。
4. **预期结果**：你能画出 `path → page → loader → 文件` 的映射表。
5. 想看真实 chunk 切分：在 `dashboard/frontend/` 下执行 `npm run build`（命令见 `package.json`），观察 `dist/assets/` 下是否为各页面生成了独立 `.js` 文件。该构建步骤**待本地验证**（需先 `npm install`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ReadonlyProvider` 必须包在 `AuthProvider` 里、而不能反过来？
**答案**：`ReadonlyProvider` 内部 `useAuth()` 读取 `token` 并把它列入 `useEffect` 依赖（token 变化时重新拉 `/api/settings`）。若反过来，`ReadonlyProvider` 取不到 Auth Context，会抛 `useAuth must be used within an AuthProvider`。

**练习 2**：`RecoverableLazyRoute` 在重试时为什么要同时调 `resetDashboardRouteLoader` 和 `setAttempt`？
**答案**：`routeLoaders` 用 `Map` 缓存了 loader 返回的 Promise，失败后该缓存仍是 rejected Promise；只 `setAttempt` 会让 `lazy()` 拿到同一个坏 Promise 再次失败。必须先清缓存，再 bump `attempt` 触发 `useMemo` 重建 `lazy(loader)`，二者缺一不可。

---

### 4.2 三道门控：鉴权、安装、只读与权限

#### 4.2.1 概念说明

「门控」指页面渲染前的拦截与重定向。本前端有三层互相独立的门：

- **鉴权门（AuthGate）**：有没有登录。
- **安装门（AuthenticatedShell）**：是不是首次安装（setup 模式）。
- **只读门 + 权限门（ReadonlyContext + accessControl）**：能不能写、能不能看某条路径。

它们分别由 `AuthContext`、`SetupContext`、`ReadonlyContext` 三个 Context 支撑，并在路由表与页面内部各自施加。理解门控的关键是：**门控不是一处集中校验，而是分散在路由 element、布局壳、页面渲染三处的接力**。

#### 4.2.2 核心流程

**鉴权门**走 `AuthContext`：

1. `AuthProvider` 挂载时，从本地存储恢复 token（`getStoredAuthToken`），调用 `installAuthenticatedFetch()` 给全局 `fetch` 装上「自动带 token + 401 广播」的拦截器。
2. `refreshSession()` 调 `fetchCurrentAuthUser()` 验证当前会话，设置 `user`。
3. 监听自定义事件 `UNAUTHORIZED_EVENT`：任何请求收到 401 都会广播它，Provider 收到后清空会话。
4. `AuthGate` 读 `isAuthenticated`：未登录则 `<Navigate to="/login">`，并把当前路径记进 `state.from`，登录后能回到原处。

**安装门**走 `SetupContext`：

1. `SetupProvider` 调 `fetchSetupState()` 拿到 `setupState.setupMode`（布尔）。
2. `AuthenticatedShell`：若 `setupMode` 为真且当前不在 `/setup`，强制重定向到 `/setup`；反之若不在 setup 模式却访问 `/setup`，重定向回 `/dashboard`。

**只读门 + 权限门**：

1. `ReadonlyProvider` 拉 `/api/settings`，得到 `readonlyMode`、`platform`、`envoyUrl` 等。
2. 路由表里每条 shell 路由都用 `canAccessDashboardPath(user, route.path)` 判定，不通过则 `<Navigate to="/dashboard">`。
3. 配置页内部再用 `canWriteConfig(user)` 决定是否进入只读。

#### 4.2.3 源码精读

`AuthGate` 的全部逻辑非常短——这就是「门」应有的样子：

[dashboard/frontend/src/app/AuthGate.tsx#L25-L30](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/app/AuthGate.tsx#L25-L30) —— 未认证时把 `pathname+search+hash` 拼成 `from` 存入 navigation state，认证后 `<LoginPage>` 可据此跳回。

`AuthenticatedShell` 的安装门——双向重定向 + 非安装态时挂 `OnboardingGuide`：

[dashboard/frontend/src/app/AuthenticatedShell.tsx#L12-L25](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/app/AuthenticatedShell.tsx#L12-L25) —— `setupMode` 期间除 `/setup` 外一切路由都被强制拉回安装向导。

`AuthContext` 的 401 自愈——任何请求被拒都会通过事件总线清会话：

[dashboard/frontend/src/contexts/AuthContext.tsx#L92-L100](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/contexts/AuthContext.tsx#L92-L100) —— 监听 `UNAUTHORIZED_EVENT`，`clearSession()` 清 token/user，使下一次渲染 `isAuthenticated` 变假、`AuthGate` 把用户踢回登录页。

权限门的核心是**「权限数组优先、角色兜底」**的双轨判定。`canAccessWithPermission` 优先看 `user.permissions[]`；只有当用户没有 permissions 数组时，才退化到按 `role`（admin/write/read）判：

[dashboard/frontend/src/utils/accessControl.ts#L31-L43](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/utils/accessControl.ts#L31-L43) —— 这套设计让系统既支持细粒度权限（与后端 RBAC 的 viewer/operator/admin + 权限位对接），又兼容只带角色的旧会话。

路径级访问控制 `canAccessDashboardPath` 按 URL 前缀分派到不同权限位，**未匹配的路径默认放行**（返回 `true`）：

[dashboard/frontend/src/utils/accessControl.ts#L83-L126](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/utils/accessControl.ts#L83-L126) —— 例如 `/config`、`/builder`、`/security`、`/fleet-sim` 都要求 `config.read`（或 read 及以上角色）；`/users`、`/ml-setup` 另有更严的权限位。

路由表里每条 shell 路由就消费这个函数，不通过即重定向到 `/dashboard`：

[dashboard/frontend/src/app/AuthenticatedAppRoutes.tsx#L113-L125](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/app/AuthenticatedAppRoutes.tsx#L113-L125) —— `canAccessDashboardPath(user, route.path) ? 渲染 : <Navigate to="/dashboard" replace/>`，把权限校验下沉到声明式路由的 element 求值里。

兜底路由还与安装态联动：

[dashboard/frontend/src/app/routeManifest.ts#L65-L67](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/app/routeManifest.ts#L65-L67) —— `fallbackRouteTarget(setupMode)`：未安装去 `/setup`，已安装去 `/dashboard`。

#### 4.2.4 代码实践

1. **实践目标**：手工推演「一个未登录用户直接访问 `/config/decisions`」的完整跳转链。
2. **操作步骤**：
   - 假设 `setupState.setupMode=false`、`isAuthenticated=false`。
   - URL `/config/decisions` 命中 `AppRouter` 的受保护分支，先进入 `AuthGate`。
   - 在 `AuthGate.tsx` 第 25-28 行：`isAuthenticated` 为假 → `<Navigate to="/login" state={{ from: '/config/decisions' }}>`。
3. **需要观察的现象**：地址栏跳到 `/login`；登录成功后若 `LoginPage` 读取 `state.from`，应跳回 `/config/decisions`。
4. **预期结果**：你能在不运行代码的情况下，说出每一跳由哪个组件、哪一行触发。
5. 想验证权限分派：阅读 `canAccessDashboardPath`，回答「一个只有 `logs.read` 权限的用户访问 `/config/models` 会怎样」——答案是被重定向到 `/dashboard`（因为 `/config` 前缀要求 `config.read`）。**结论可直接从源码得出，无需运行。**

#### 4.2.5 小练习与答案

**练习 1**：`canAccessDashboardPath` 对未列出的路径（如 `/playground`）返回什么？这样设计有什么好处与风险？
**答案**：返回 `true`（默认放行）。好处是新增页面若无需特殊权限，不必改访问控制函数；风险是若开发者忘了为新敏感页加前缀分支，它会意外对所有人开放。`/playground` 正是默认放行的例子。

**练习 2**：`setupMode=true` 时，用户访问 `/playground` 会发生什么？
**答案**：先过 `AuthGate`（假设已登录），再进 `AuthenticatedShell`；因 `setupMode` 为真且 `pathname !== '/setup'`，被 `<Navigate to="/setup" replace>`。即安装模式下除 `/setup` 一切都被拦。

---

### 4.3 概览页：DashboardPage 的数据塑造

#### 4.3.1 概念说明

概览页（`/dashboard`）是登录后的默认着陆页，它的任务是「一屏看清路由器的形状与健康度」。它合并**两类异构数据**：

- **配置数据**（来自 `/api/router/config/all`）：有多少 signals/decisions/models/plugins，决策如何分类。
- **运行时状态**（来自 `/api/status`）：服务是否健康、模型装载进度、router 启动阶段。

这两类数据更新频率不同（状态比配置变得快），所以页面用**两套独立的轮询节奏**分别拉取，并在前端把配置「数」成统计、把状态「翻译」成色调与文案。

#### 4.3.2 核心流程

1. 挂载时 `fetchAll()` 并发拉 config 与 status（首次允许在隐藏 tab 也拉）。
2. 注册两个定时器：status 每 10s、config 每 30s。
3. 监听 `visibilitychange`：页面重新可见时立即各拉一次（避免后台空跑，回到前台秒级刷新）。
4. 监听自定义事件 `config-deployed`：配置被部署后立即全量刷新（配置页保存并部署后会广播此事件）。
5. 用 `useMemo` 把 config 数成 `signalStats / decisionCount / modelCount / pluginCount`，把 status 翻译成 `modelStatus`（value/detail/tone 三元组）与健康服务计数。
6. 渲染：统计卡片区（可点击跳转）+ Intelligence Layers 流程图 + System Health + Loaded Models + Signal Breakdown + Decisions Overview。

数据塑造全部抽到纯函数模块（`dashboardPageStats.ts`、`dashboardPageOverview.ts`、`utils/routerRuntime.ts`），组件只负责编排，便于单测。

#### 4.3.3 源码精读

取数与轮询的副作用——两个节奏、三类触发（定时/可见性/部署事件）：

[dashboard/frontend/src/pages/DashboardPage.tsx#L73-L105](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/DashboardPage.tsx#L73-L105) —— `statusInterval=10000`、`configInterval=30000`；`config-deployed` 事件触发 `fetchAll()`；可见性变化触发 `pollStatus()/pollConfig()`。`createVisibilityAwareRequest` 包装了「隐藏 tab 内降级」的逻辑。

两类数据来源分明——config 走 `/api/router/config/all`，status 走 `/api/status`：

[dashboard/frontend/src/pages/DashboardPage.tsx#L37-L51](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/DashboardPage.tsx#L37-L51) —— 两个 `fetchCallback` 各自只管取数与塞 state，错误与节奏交给外层 `fetchAll`。

配置「数」成统计——纯函数同时兼容 canonical（`routing.signals`）与旧（顶层 `signals`）两种结构：

[dashboard/frontend/src/pages/dashboardPageStats.ts#L3-L16](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/dashboardPageStats.ts#L3-L16) —— `cfg.routing?.signals ?? cfg.signals` 的兜底写法贯穿所有计数函数，是前端兼容多版本配置格式的统一手法。

决策按优先级分三类——`priority>=999` 为 guardrail、`<=100` 为 fallback、其余为 routing：

[dashboard/frontend/src/pages/dashboardPageStats.ts#L46-L51](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/dashboardPageStats.ts#L46-L51) —— 这与 u2-l4 讲的 TIER/PRIORITY 语义呼应：高优先级守门、低优先级兜底，中间是普通路由。

状态「翻译」成色调——把 router 启动阶段与 overall 健康度映射成 `ok/warn/down` 三态：

[dashboard/frontend/src/utils/routerRuntime.ts#L239-L313](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/utils/routerRuntime.ts#L239-L313) —— `getModelStatusSummary` 在 `downloading_models` 阶段返回 `ready/total` 进度、`error` 阶段返回「Error」、健康时返回「Ready」。这正是 u4-l1 讲的 `starting→downloading_models→initializing_models→ready` 启动状态机在前端的映射。

统计卡片把数字与导航耦合——每张卡都是 `<button>`，点击跳到对应详情页：

[dashboard/frontend/src/pages/DashboardPage.tsx#L184-L210](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/DashboardPage.tsx#L184-L210) —— Models 卡跳 `/config/models`、Decisions 卡跳 `/config/decisions`，把概览页当作「总览 + 入口聚合」。

#### 4.3.4 代码实践

1. **实践目标**：理解概览页如何把一份配置「数」成 Signal Breakdown 柱状图。
2. **操作步骤**：
   - 读 `DashboardPage.tsx` 的 `signalStats`（107 行）与 `signalBreakdownRows`（124-127 行）。
   - 跟进 `buildSignalBreakdownRows`（`dashboardPageOverview.ts`）：它对 `byType` 排序、按最大计数算百分比、从 `SIGNAL_COLORS` 取色。
   - 再看 `countSignals` 如何把 `signals` 对象的每个数组字段计成 `byType[type] = arr.length`。
3. **需要观察的现象**：Signal Breakdown 的每根柱子宽度 = `count / maxCount * 100%`，颜色由信号类型决定（如 `pii` 红、`keywords` 青）。
4. **预期结果**：你能解释「为什么柱子最长的那根总是 100%、其余按比例缩短」。
5. 想在本地看真实渲染：需先按 u13-l1/u1-l3 起完整后端栈（router + dashboard backend），再访问 dashboard。若仅想验证纯函数逻辑，可执行 `npm run test:unit`（见 `package.json` 的 `test:unit` 脚本，用 vitest）。该测试运行**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 status 轮询 10s 而 config 轮询 30s？
**答案**：状态（服务健康、模型装载）变化更频繁且对运维更关键，需要更短间隔；配置变更相对低频，且已有 `config-deployed` 事件做即时推送，30s 轮询只是兜底，过长间隔能减少无谓请求。

**练习 2**：`getModelStatusSummary` 在 `overall='not_running'` 与 `overall='degraded'` 时分别返回什么 tone？
**答案**：`not_running`/`stopped` → `{value:'Offline', tone:'down'}`；`degraded` → `{value:'Degraded', tone:'warn'}`；健康 → `{value:'Ready', tone:'ok'}`。tone 决定了统计卡图标的着色类。

---

### 4.4 配置页：ConfigPage 的分段渲染与读写

#### 4.4.1 概念说明

配置页（`/config` 与 `/config/:section`）是面板的编辑中枢，对应 u13-l1 后端的 config/deploy handler。它的特点有三：

1. **分段渲染**：单个 `ConfigPage` 组件按 `activeSection` 切换到八个子段组件（signals/decisions/projections/models/entrypoints-recipes/global-config/mcp/topology），每段是独立组件。
2. **规范化双向**：读时把后端 canonical 配置「投影」成管理器友好的 `ConfigData`（`projectCanonicalConfigForManager`），写时再「规范化」回去（`canonicalizeConfigForManagerSave`），并兼容 legacy 字段名。
3. **只读协作**：`configReadonly = isReadonly || !canWriteConfig(user)`，只读时禁用所有保存与编辑入口。

#### 4.4.2 核心流程

1. URL `/config/:section` 命中 `ConfigSectionRoute`，它把路径段（如 `decisions`、`routes`、`endpoints`）归一化映射成 `ConfigSection`，并把 `activeSection` 通过 props 透传给懒加载的 `ConfigPage`。
2. `ConfigPage` 挂载：非 MCP 段时并发 `fetchConfig()`（`/api/router/config/all`）与 `fetchRouterDefaults()`（`/api/router/config/global`）。
3. 读到的 canonical 数据先经 `projectCanonicalConfigForManager` 投影，再 `detectConfigFormat` 判断是 `python-cli` 还是 `legacy`。
4. `renderActiveSection()` 按 `activeSection` switch 到对应子段组件；所有子段共用 `saveConfig`/`openEditModal`/`openViewModal` 三个回调。
5. 用户编辑触发 `saveConfig(updatedConfig)`：先 `canonicalizeConfigForManagerSave` 规范化，`POST /api/router/config/update`，成功后 `fetchConfig()` 刷新。
6. `configReadonly` 为真时，`saveConfig` 直接抛错，`ViewModal` 的「编辑」按钮也被隐藏（`onEdit={configReadonly ? undefined : ...}`）。

#### 4.4.3 源码精读

URL 段 → `ConfigSection` 的归一化映射，兼容历史别名（`routes`→decisions、`endpoints`→models）：

[dashboard/frontend/src/app/ConfigSectionRoutes.tsx#L32-L52](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/app/ConfigSectionRoutes.tsx#L32-L52) —— `ConfigSectionRoute` 在 `useEffect` 里把 section 同步进父级 state，使左侧导航高亮与 URL 保持一致。

八段导航的权威定义——`ConfigSection` 类型与导航项都在此：

[dashboard/frontend/src/components/ConfigNav.tsx#L5-L13](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/components/ConfigNav.tsx#L5-L13) —— 注意每个 section 都注释了对应的 `config.yaml` 段（如 `projections` ↔ `routing.projections`），与 u3-l1 的七大顶层段一一对应。

分段渲染的 switch——单一组件、八条分支、每分支委托子组件：

[dashboard/frontend/src/pages/ConfigPage.tsx#L430-L449](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/ConfigPage.tsx#L430-L449) —— 这是最典型的「容器/展示分离」：`ConfigPage` 只管取数、规范化、只读判定与模态态，业务表单全在各 `ConfigPageXxxSection` 里。

只读判定——双因素合取，任一为真即只读：

[dashboard/frontend/src/pages/ConfigPage.tsx#L38-L47](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/ConfigPage.tsx#L38-L47) —— `configReadonly = isReadonly || !canWriteConfig(user)`：要么后端设置了 `readonlyMode`，要么当前用户无 `config.write` 权限。

保存链路——规范化 → POST → 刷新，且只读时硬拦截：

[dashboard/frontend/src/pages/ConfigPage.tsx#L171-L212](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/ConfigPage.tsx#L171-L212) —— `configReadonly` 时直接抛 `'Dashboard is in read-only mode...'`；否则 `canonicalizeConfigForManagerSave` 后 `POST /api/router/config/update`，并细致解析错误体（先试 JSON 取 `error/message`，否则用原文）。

规范化兼容层——legacy 字段名到 canonical 的映射表：

[dashboard/frontend/src/pages/configPageCanonicalization.ts#L10-L26](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/frontend/src/pages/configPageCanonicalization.ts#L10-L26) —— 例如 `keyword_rules`→`keywords`、`categories`→`domains`、`embedding_rules`→`embeddings`，使前端能同时新旧两版配置格式读写。这与 u3-l3 讲的 canonical 规范化在后端的方向一致，前端这层是为「管理器友好视图」做的二次整形。

#### 4.4.4 代码实践

1. **实践目标**：在配置页加一个只读日志，验证保存链路与只读门。
2. **操作步骤**：
   - 在 `ConfigPage.tsx` 的 `saveConfig` 函数开头（约 173 行 `if (configReadonly)` 之后）加一行 `console.info('[tutorial] saving config, format=', configFormat)`。
   - 阅读 `renderActiveSection`，确认 `/config/projections` 会走到 `renderProjectionsSection()`。
3. **需要观察的现象**：
   - 当 `configReadonly` 为真（如 `readonlyMode` 或无 `config.write`），点击保存会在控制台**看不到**那条 info 日志——因为在它之前就抛错返回了，证明只读门生效于保存链路最前端。
   - 非只读时保存，控制台会打印当前配置格式（`python-cli` 或 `legacy`）。
4. **预期结果**：你用一个 `console.info` 就验证了「只读拦截优先于规范化与网络请求」这一顺序保证。
5. 此修改仅是临时调试日志，**请勿提交**；它不改变任何行为，仅用于观察。运行前端需完整后端栈，本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 MCP 段（`activeSection === 'mcp'`）会跳过 `fetchConfig`？
**答案**：MCP 段由独立的 `ConfigPageMCPSection` 管理，它有自己的数据源（MCP 服务端与工具库），不依赖整份 router config。`ConfigPage` 在 `useEffect` 里对 `isMCPSection` 提前 return（83-87 行），且初始 `loading` 也设为 `!isMCPSection`，避免无谓加载与闪烁。

**练习 2**：用户编辑配置点保存后，概览页（DashboardPage）会自动刷新吗？通过什么机制？
**答案**：会。配置部署后会广播 `config-deployed` 自定义事件，`DashboardPage` 监听了该事件并触发 `fetchAll()`。两个页面不直接耦合，靠 window 事件总线协作——前提是保存动作最终触发了部署（参见 u13-l1 的 deploy handler 与运行时下发）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「加一个新导航项」的端到端追踪：

**任务**：假设要新增一个只读页面 `/usage`（用量分析），按本前端的架构约定，它需要改动哪几处？

**参考思路**（请对照源码验证，不要直接抄）：

1. **路由表**：在 `routeManifest.ts` 的 `shellRouteDefinitions` 加 `{ path: '/usage', page: 'usage' }`，并把 `'usage'` 加入 `ShellRoutePage` 联合类型。
2. **懒加载入口**：在 `routeLoaders.ts` 加 `loadUsagePage = () => import('../pages/UsagePage')`，并在 `routeLoaders` 数组加匹配项（供预加载），在 `AuthenticatedAppRoutes.tsx` 的 `shellPageElements` 加映射。
3. **权限门**：决定 `/usage` 该用什么权限位。若它属于运维只读类，可在 `accessControl.ts` 的 `canAccessDashboardPath` 加一条前缀分支（如要求 `logs.read`）；若公开，则依赖默认放行，并评估其安全性。
4. **页面**：新建 `pages/UsagePage.tsx`，参考 `DashboardPage` 的取数与轮询骨架。
5. **导航**：在左侧导航（`Layout`/`LayoutMegaMenu` 相关组件）加入口。

**验证要点**：
- 未登录访问 `/usage` 应被 `AuthGate` 踢到 `/login`（带 `from`）。
- 登录但 setup 模式下应被 `AuthenticatedShell` 踢到 `/setup`。
- 无权限应被 `canAccessDashboardPath` 踢到 `/dashboard`。
- 三道门按「鉴权 → 安装 → 权限」的顺序作用，互不重复。

完成后，用一句话总结：本前端的「加页面」成本主要落在**声明式路由表 + 懒加载入口 + 权限前缀**三处数据/配置，而非散落的命令式分支。

## 6. 本讲小结

- 前端是「Provider 桶 → 路由壳 → 门控 → 布局壳 → 懒加载页面」的分层结构，`App.tsx` 只管 Provider 顺序、`AppRouter` 只管路由组织。
- 三道门控分工：`AuthGate`（登录）、`AuthenticatedShell`（安装模式）、`accessControl` + `ReadonlyContext`（权限与只读），分散在路由 element、布局壳、页面内部接力执行。
- 路由表是声明式数据驱动的（`routeManifest` + `routeLoaders`），`RecoverableLazyRoute` 统一了 Suspense 与失败重试，`preloadDashboardRoute` 提供悬停预加载。
- 概览页合并配置（`/api/router/config/all`）与状态（`/api/status`）两类数据，两套轮询节奏（10s/30s）+ 可见性 + `config-deployed` 事件，统计塑造全抽到纯函数模块。
- 配置页按 `activeSection` 分八段渲染，读写经 `project/canonicalize` 双向规范化（兼容 legacy），`configReadonly` 在保存链路最前端硬拦截。
- 权限判定遵循「权限数组优先、角色兜底」双轨，与后端 RBAC 的 viewer/operator/admin + 权限位对接。

## 7. 下一步学习建议

- **可视化编辑**：配置页的表单编辑之外，还有可视化构建器与 DSL 编辑器，建议继续学 **u13-l3 可视化配置与 DSL 编辑器**，理解 `BuilderPage` 与 `DslEditorPage` 如何与 DSL 编译/反编译（u7-l2）双向联动。
- **后端契约回看**：本前端消费的 `/api/router/config/*`、`/api/status`、`/api/settings` 端点来自 **u13-l1 面板后端** 与 **u11-l1 API Server**；遇到字段含义不明时回查这两讲。
- **源码延伸阅读**：`dashboard/frontend/src/app/AppShellLayout.tsx`（布局壳与导航装配）、`components/Layout*.tsx`（mega menu 与移动端导航）、`contexts/authSession.ts`（会话判定细节）可补全本讲未展开的壳内细节。
