# 测验应用架构（Vue.js）

## 1. 本讲目标

本讲是「配套工具与维护机制」单元的第一篇。前面 5 个单元我们都在学习 AI 课程本身的内容，从这一篇开始，我们转向仓库里**支撑课程运转的工具链**。

AI-For-Beginners 课程里每一课都配有「课前测验（Pre-Quiz）」和「课后测验（Post-Quiz）」。这些测验不是写死在 Markdown 里的，而是由一个独立的 **Vue.js 单页应用（SPA）** 负责渲染和交互。这个应用位于 [etc/quiz-app/](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app) 目录。

学完本讲，你应当能够：

- 看懂 `quiz-app` 的项目结构、依赖与 npm 脚本，知道每个目录/文件扮演什么角色。
- 说清一条完整的**启动链路**：浏览器打开 `index.html` → `main.js` 创建 Vue 实例 → 路由决定渲染哪个页面组件。
- 读懂 `Quiz.vue` 组件，理解它如何根据 URL 里的测验编号、从 JSON 题库里取出对应题目并渲染、判分。
- 理解这个应用里**国际化（i18n）**的真实实现方式（以及它和标准 `vue-i18n` 用法的微妙差别）。

> 本讲假设你已经读过 [u1-l3 开发环境搭建](u1-l3-environment-setup.md)，知道什么是命令行、什么是依赖管理。本讲会用到 `npm`，但不要求你 previously 写过 Vue。

## 2. 前置知识

### 什么是前端框架与 Vue

浏览器原生只认识 HTML、CSS、JavaScript。直接用这三件套写复杂页面，代码会很快变成「查找 DOM 元素 → 手动改内容」的意大利面条。**前端框架**（如 Vue、React、Angular）的核心理念是：你只需声明「数据长什么样、页面应该长什么样」，框架负责在数据变化时自动更新页面。这种「数据驱动视图」的思想，和你在 [u2-l4](u2-l4-own-framework.md) 里见过的「前向传播算输出」有异曲同工之处——都是**单向数据流**。

**Vue.js**（法语 view，意为「视图」）是其中的轻量级代表。本仓库的 quiz-app 用的是 **Vue 2**（`2.6.11` 版本），其标志性的写法叫**单文件组件（SFC, Single-File Component）**：把一个组件的模板（HTML）、逻辑（JS）、样式（CSS）写在同一个 `.vue` 文件里。

一个 `.vue` 文件通常有三块：

```vue
<template> <!-- 1. 模板：声明这个组件长什么样 --> </template>
<script>   <!-- 2. 逻辑：数据与方法 --> </script>
<style>    <!-- 3. 样式：外观 --> </style>
```

### 什么是单页应用（SPA）与路由

传统网站点一个链接，服务器就返回一个**全新**的 HTML 页面。**单页应用（Single-Page Application, SPA）** 则不同：服务器只返回**一个** HTML 骨架，之后所有「换页」都由 JavaScript 在浏览器里完成，不刷新整个页面。负责「根据网址（URL）决定显示哪个组件」的机制，就叫**路由（router）**。本应用用 `vue-router` 实现。

> 关键直觉：在 SPA 里，地址栏的 URL 变化**不会**触发服务器请求，而是被路由拦截，换成「切换组件」。例如访问 `/quiz/101` 时，路由会让 `Quiz.vue` 组件显示，并把 `101` 作为参数传给它。

### 什么是 i18n

**i18n** 是 **internationalization**（国际化）的缩写——单词首尾 `i` 和 `n` 之间有 18 个字母，故得名。它指「让同一个程序能展示多种语言」。本应用的题目文本以 `en`（英语）和 `es`（西班牙语）两种语言存放，用户可在右上角下拉框切换。

## 3. 本讲源码地图

本讲涉及的文件都在 `etc/quiz-app/` 下，按下表分类理解：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| `package.json` | 声明依赖与 npm 脚本 | 入口地图、运行命令来源 |
| `public/index.html` | 唯一的 HTML 骨架，含 `<div id="app">` | 启动链路的起点 |
| `src/main.js` | 创建 Vue 实例并挂载 | 启动链路的核心 |
| `src/router/index.js` | 定义 3 条路由规则 | 路由与启动链路 |
| `src/App.vue` | 根组件：导航栏 + 语言下拉 + `<router-view>` | 启动链路 + i18n |
| `src/views/Home.vue` | 首页：列出所有测验的链接 | 路由跳转的发起方 |
| `src/components/Quiz.vue` | 测验组件：渲染题目、判分 | 本讲的核心精读对象 |
| `src/views/NotFound.vue` | 404 兜底页 | 路由的边界情况 |
| `src/assets/translations/index.js` | 汇总各语言题库 | i18n 数据入口 |
| `src/assets/translations/en/index.js` | 把 24 课的 JSON 编号成 0–23 | 数据形状的关键映射 |
| `src/assets/translations/en/lesson-1.json` | 第 1 课的真实题目（范例） | 题库的数据结构样板 |
| `babel.config.js` | Babel 转译配置 | 工程配置（略读） |
| `public/routes.json` | 静态托管的 SPA 回退规则 | 部署相关 |

> 记忆口诀（承接 [u1-l2](u1-l2-directory-structure.md) 的口诀）：**「看测验 → etc」**。这个目录里，`public/` 是浏览器加载的入口，`src/` 是源码，`src/assets/translations/` 是题库数据。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Vue 项目结构** —— 认识依赖、脚本与目录分工。
2. **路由与启动链路** —— 从浏览器地址栏到组件渲染的完整过程。
3. **测验组件与 i18n** —— `Quiz.vue` 如何渲染题目、判分，以及多语言如何生效。

### 4.1 Vue 项目结构

#### 4.1.1 概念说明

一个现代前端项目不只是「几个 `.vue` 文件」，它背后还有一套**工程化骨架**：

- **包管理器 `npm`**：把别人写好的库（Vue、vue-router 等）下载到本地 `node_modules/`。依赖清单写在 `package.json` 里。
- **构建工具**：浏览器无法直接运行 `.vue` 文件（它不是标准 JS），需要 `@vue/cli-service` 把 `.vue` 编译成普通 HTML/JS/CSS，这一步叫**打包（build）**。开发时还会启动一个**热更新开发服务器（dev server）**，让你改完代码浏览器自动刷新。
- **转译器 Babel**：让你能用较新的 JS 语法写代码，再转成老浏览器也认识的语法。

理解这套骨架，等于拿到了「读懂任何 Vue 项目」的通用钥匙。

#### 4.1.2 核心流程

```
package.json 声明依赖 + 脚本
        │
        ▼
npm install  ──► 下载依赖到 node_modules/
        │
        ▼
npm run serve ──► vue-cli-service 启动开发服务器（热更新）
npm run build ──► vue-cli-service 打包出可部署的 dist/
npm run lint  ──► 检查代码风格
```

`package.json` 里的 `scripts` 字段是关键：它把一长串命令起个短名字。当你敲 `npm run serve`，npm 实际执行的是 `vue-cli-service serve`。

#### 4.1.3 源码精读

先看依赖与脚本，定位整个项目的「能力清单」。

[etc/quiz-app/package.json:5-9](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/package.json#L5-L9) 定义了三个 npm 脚本——`serve` 开发、`build` 打包、`lint` 检查，全部委托给 `vue-cli-service`。

[etc/quiz-app/package.json:10-15](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/package.json#L10-L15) 是运行时依赖（`dependencies`），揭示了本应用的三件核心武器：

| 依赖 | 版本 | 作用 |
|------|------|------|
| `vue` | `^2.6.11` | 框架本体（注意是 **Vue 2**，不是 Vue 3） |
| `vue-router` | `^3.4.9` | 单页应用路由 |
| `vue-i18n` | `^8.28.2` | 国际化插件（Vue 2 配套的 v8 版本） |
| `core-js` | `^3.6.5` | JS 新特性的 polyfill（垫片） |

> 版本号前的 `^` 表示「兼容该大版本下的更高小版本」。`vue` 和 `vue-template-compiler`、`vue-router` 和 `vue` 之间必须**版本配套**（Vue 2 配 router v3、i18n v8），这是后续维护时最容易踩的坑。

`devDependencies`（[第 16–24 行](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/package.json#L16-L24)）则是只在开发时才需要的工具：`@vue/cli-service` 负责编译打包，ESLint 负责检查代码风格。注意 `package.json` 里还内联了一段 `eslintConfig`，所以这个项目**不需要单独的 `.eslintrc` 文件**。

再看浏览器加载的 HTML 骨架。

[etc/quiz-app/public/index.html:14](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/public/index.html#L14) 只有一行关键内容：`<div id="app"></div>`。整个 quiz-app 最终都会被「注入」到这个空 div 里。其余的 `<%= ... %>` 是 webpack 的模板语法，在打包时被替换成真实标题等。

#### 4.1.4 代码实践

**实践目标**：不启动应用，仅靠阅读，建立「文件 → 角色」的映射。

**操作步骤**：

1. 打开 [etc/quiz-app/package.json](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/package.json)，列出 `dependencies` 与 `devDependencies` 各自的成员。
2. 在仓库里用文件浏览器查看 `etc/quiz-app/` 的目录树，把 `public/`、`src/`、`src/components/`、`src/views/`、`src/assets/translations/` 与本讲「源码地图」表格逐项对照。

**需要观察的现象**：`src/` 下有 `components/` 和 `views/` 两个放组件的目录。

**预期结果**：`views/` 放「页面级」组件（Home、NotFound，对应一整页），`components/` 放「可复用零件」组件（Quiz）。这是 Vue 社区常见的目录约定，并非强制。

**待本地验证**：目录约定是社区习惯，不同项目可能略有差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vue` 放在 `dependencies`，而 `@vue/cli-service` 放在 `devDependencies`？

> **答案**：`vue` 是应用运行时**必须**的库（浏览器里跑的代码会 `import Vue`），所以放在 `dependencies`；`@vue/cli-service` 只在**开发/打包**时用来编译 `.vue`，打包产物（`dist/`）里已经不含它，线上运行不需要，故放在 `devDependencies`。

**练习 2**：如果有人把这个项目升级到 Vue 3，`vue-router` 和 `vue-i18n` 的版本需要怎样调整？

> **答案**：需要配套升级——Vue 3 对应 `vue-router` v4、`vue-i18n` v9。Vue 2/v3 的 API 差异较大（如 Vue 3 用 `createApp` 而非 `new Vue`），所以这不是简单改版本号，而是要改写 `main.js` 等启动代码。

---

### 4.2 路由与启动链路

#### 4.2.1 概念说明

「启动链路」回答一个具体问题：**当用户在浏览器输入一个网址，到屏幕上出现测验，中间发生了什么？** 这条链路涉及三个文件的接力：

1. `public/index.html` 提供空壳 `<div id="app">`。
2. `src/main.js` 创建 Vue 实例，把它「挂载（mount）」到那个 div 上。
3. `src/router/index.js` 根据当前网址，决定 `<router-view>` 里显示哪个组件。

理解这条链路，就理解了所有 SPA 应用的通用启动模式。

#### 4.2.2 核心流程

```
浏览器加载 index.html
        │  执行打包注入的 JS（即编译后的 main.js）
        ▼
main.js: new Vue({ i18n, router, render: h => h(App) }).$mount('#app')
        │                    │
        │                    └─ 把 App.vue 作为根组件渲染进 <div id="app">
        ▼
App.vue 模板里写着 <router-view>
        │
        ▼
router 读取地址栏 URL，匹配路由表：
   /            → Home.vue      （列出所有测验链接）
   /quiz/:id    → Quiz.vue      （渲染 id 对应的测验）
   其它          → NotFound.vue  （404）
```

注意 `:id` 是**动态路由参数**：`/quiz/101` 里 `101` 会被捕获为 `id`，组件可通过 `this.$route.params.id` 读取它。

#### 4.2.3 源码精读

**① 启动入口 `main.js`**

[etc/quiz-app/src/main.js:1-14](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/main.js#L1-L14) 是整个应用的「发令枪」。逐行拆解：

- 第 1–2 行：`import Vue` 与 `import App`，引入框架和根组件。
- 第 4 行：`import router from './router'`，引入路由实例。
- 第 6–7 行：`Vue.use(VueI18n)` 是 Vue 的**插件注册**语法，表示「给所有组件启用 i18n 能力」。
- 第 9–12 行：创建 `VueI18n` 实例，默认语言 `locale: 'en'`，兜底语言 `fallbackLocale: 'en'`。
- [第 14 行](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/main.js#L14)：**最关键的一行**——`new Vue({ i18n, router, render: (h) => h(App) }).$mount('#app');`
  - `new Vue(...)` 创建根实例；
  - `i18n, router` 把这两件「全局能力」注入应用；
  - `render: (h) => h(App)` 声明「根实例渲染的是 `App.vue`」（`h` 是创建虚拟 DOM 的函数）；
  - `.$mount('#app')` 把渲染结果挂到 `index.html` 里那个 `<div id="app">` 上。

> 这行代码是 Vue 2 应用的标准启动句式。对比 Vue 3 会写成 `createApp(App).use(router).use(i18n).mount('#app')`——同样的意思，不同写法。

**② 路由表 `router/index.js`**

[etc/quiz-app/src/router/index.js:8-29](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/router/index.js#L8-L29) 定义了三条路由规则。重点看：

- 第 9 行 `mode: 'history'`：启用 HTML5 History 模式，使 URL 长得像 `/quiz/101`（干净的路径），而不是带 `#` 的 `/#/quiz/101`（hash 模式）。代价是**部署时需要配置服务器回退**（见本节末尾 `routes.json`）。
- [第 18–21 行](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/router/index.js#L18-L21)：动态路由 `/quiz/:id`，匹配组件 `Quiz`。访问 `/quiz/101` 时，`101` 就是 `:id`。
- [第 22–27 行](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/router/index.js#L22-L27)：通配路由 `/:pathMatch(.*)*`，匹配任何未命中的路径，显示 `NotFound`。这是 404 兜底。

**③ 首页如何发起跳转 `Home.vue`**

首页用 `v-for` 循环生成一串链接：

[etc/quiz-app/src/views/Home.vue:5-12](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/views/Home.vue#L5-L12) 用 `<router-link :to="\`quiz/${quiz.id}\`">` 为每个测验生成一个链接，地址形如 `quiz/101`。点击它，路由就把 `101` 塞进 `:id`，切到 `Quiz.vue`。

**④ history 模式与部署回退 `routes.json`**

由于用了 `mode: 'history'`，若用户直接刷新 `/quiz/101`，服务器会去找一个叫 `quiz/101` 的文件——但它不存在（这是 SPA，所有内容都在 `index.html` 里动态生成）。所以静态托管时必须加一条回退规则。

[etc/quiz-app/public/routes.json:1-8](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/public/routes.json#L1-L8) 正是此意：把所有路径（`/*`）都回退到 `/index.html`，交给前端路由处理。（这是 Azure Static Web Apps 的约定文件。）

#### 4.2.4 代码实践

**实践目标**：在本地把 quiz-app 跑起来，亲手走一遍启动链路。这是本讲的主实践任务。

**操作步骤**：

1. 确认已安装 Node.js 与 npm（可用 `node -v` 与 `npm -v` 检查；若未装，参考 [u1-l3](u1-l3-environment-setup.md) 的环境思路，但这里需要的是 Node 而非 conda）。
2. 进入应用目录：`cd etc/quiz-app`
3. 安装依赖：`npm install`
4. 启动开发服务器：`npm run serve`
5. 终端会打印一个本地地址（通常是 `http://localhost:8080`），用浏览器打开。
6. 在首页点击任一测验链接（地址会变成 `/quiz/101` 之类），观察页面切换**没有整页刷新**。
7. 手动把地址栏改成 `/quiz/201` 回车，观察显示的是另一套测验。
8. 把地址栏改成 `/quiz/999999`（不存在的编号），观察显示的是题目为空或空白（因为找不到匹配的测验）。

**需要观察的现象**：

- `npm install` 会下载较多包，可能耗时几分钟，并在目录下生成 `node_modules/`。
- `npm run serve` 启动后，修改任意 `.vue` 文件并保存，浏览器会自动热更新。
- 切换测验时地址栏变化、但页面不闪动（这是 SPA 的标志）。

**预期结果**：能看到首页的测验列表，点进去能答题；答对前进、答错提示重试。

**待本地验证**：具体端口号、依赖安装时长因机器与网络而异；若 `npm install` 报版本冲突，可能与较新的 Node 版本有关，记录报错信息以便排查。

> 提示：本仓库的 quiz-app 用的是 Vue 2 与较老的 CLI 版本，在新版 Node 上偶有兼容性警告。若仅作学习用途，可记录告警而不必逐一修复。**请不要修改仓库源码**，如需实验请在你自己的 fork/副本中进行。

#### 4.2.5 小练习与答案

**练习 1**：为什么访问 `/quiz/999999` 不会显示 `NotFound`，而是显示空白？

> **答案**：`/quiz/999999` **命中了** `/quiz/:id` 这条路由（`999999` 是合法的 `:id`），所以渲染的是 `Quiz.vue` 而非 `NotFound.vue`。`NotFound` 只在**完全没有任何路由匹配**时才出现（比如访问 `/foobar`）。`Quiz.vue` 内部用 `v-if="route == quiz.id"` 过滤，找不到编号为 `999999` 的测验，于是什么都不渲染，呈现空白。

**练习 2**：如果把 `mode: 'history'` 删掉（回到默认 hash 模式），URL 会变成什么样？

> **答案**：地址会带上 `#`，变成 `/#/quiz/101`。hash 模式不需要服务器配置回退（`#` 后面的内容不发给服务器），但 URL 不如 history 模式美观。

---

### 4.3 测验组件与 i18n

#### 4.3.1 概念说明

这是本讲最核心的模块。`Quiz.vue` 要解决两个问题：

1. **渲染**：从一堆 JSON 题库里，找到当前 URL 对应的那一套题，把题目和选项画成按钮。
2. **判分**：用户点一个选项，判断对错——对就进入下一题，错就提示重试；答完规定题数显示完成。

同时，整个应用还要支持**中英文等切换**。这里有一个值得注意的设计细节：虽然项目装了标准的 `vue-i18n` 插件，但题目的多语言**并没有**用 `vue-i18n` 的 `$t()` 翻译函数，而是**手动用对象下标** `questions[currLocale]` 来切换语言包。`vue-i18n` 在这里主要被当作「全局共享当前语言」的状态容器使用。这是一个真实的、略带「历史包袱」的实现，看懂它正是源码阅读的价值所在。

#### 4.3.2 核心流程

**数据形状**（先看数据，再看代码）：

题库是一个嵌套对象，结构如下（以 [lesson-1.json](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/lesson-1.json) 为例）：

```
messages = {                      // translations/index.js 汇总
  en: {                           // translations/en/index.js
    0: {                          // lesson-1.json 的 [0]
      title: "AI for Beginners: Quizzes",
      complete: "Congratulations, ...",
      error: "Sorry, try again",
      quizzes: [                  // 一课含多套测验（课前/课后）
        { id: 101, title: "...Pre Quiz",  quiz: [题目1, 题目2, 题目3] },
        { id: 201, title: "...Post-Quiz", quiz: [题目1, 题目2, 题目3] }
      ]
    },
    1: { ... lesson-2 ... },
    ...
    23: { ... lesson-24 ... }
  },
  es: { 0: { ...西语版第1课... } }   // es 目前只翻译了第 1 课
}
```

每个题目形如：

```json
{
  "questionText": "...",
  "answerOptions": [
    { "answerText": "A", "isCorrect": false },
    { "answerText": "B", "isCorrect": true  }
  ]
}
```

**渲染与判分流程**：

```
URL /quiz/101
   │  router 把 101 放进 $route.params.id
   ▼
Quiz.vue created(): this.route = "101"
   │
   ▼
模板 v-for 遍历所有测验，v-if="route == quiz.id" 只显示 id==101 的那套
   │
   ▼
显示 quiz.quiz[currentQuestion] 的题目与选项按钮
   │  用户点某选项
   ▼
handleAnswerClick(option.isCorrect):
   答对 → currentQuestion+1；若已是第 3 题 → complete=true（显示恭喜）
   答错 → error=true（显示「Sorry, try again」）
```

#### 4.3.3 源码精读

**① 数据如何被汇总与编号**

[etc/quiz-app/src/assets/translations/index.js:1-8](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/index.js#L1-L8) 把英语和西班牙语两个语言包合并成 `{ en, es }`。

[etc/quiz-app/src/assets/translations/en/index.js:25](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/index.js#L25) 是数据形状的关键映射：它把 24 个 lesson JSON 导入后，**重新用 0–23 作为键**，并取每个文件数组的第 0 个元素（`x1[0]`）。所以代码里访问题目用的是 `messages[locale][0]`、`messages[locale][1]`……，而不是按文件名。

> 小坑：这里键是 `0..23`（24 课），但 README 里说「共 40 套测验、从 0 开始计数」——这是按**测验（pre/post）**计的，而数据文件是按**课**组织的（每课一份 JSON 内含多套测验）。两套口径别混淆。

**② `Quiz.vue` 的模板与数据**

[etc/quiz-app/src/components/Quiz.vue:3-6](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/components/Quiz.vue#L3-L6) 用两层 `v-for` 遍历：外层 `q in questions[currLocale]` 遍历某语言下的每一课，内层 `quiz in q.quizzes` 遍历该课的每套测验；再用 `v-if="route == quiz.id"` 只放行当前 URL 对应的那一套。这是一种「遍历全部、靠条件过滤」的写法——简单直接，但每套测验都要遍历到。

[etc/quiz-app/src/components/Quiz.vue:16-29](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/components/Quiz.vue#L16-L29) 渲染当前题目（`quiz.quiz[currentQuestion].questionText`）和选项按钮，点击时调用 `handleAnswerClick(option.isCorrect)`。

[etc/quiz-app/src/components/Quiz.vue:42-50](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/components/Quiz.vue#L42-L50) 是组件的本地状态：`currentQuestion`（当前第几题，从 0 起）、`complete`（是否完成）、`error`（是否答错）、`route`（当前测验编号）、`locale`。

**③ 判分逻辑 `handleAnswerClick`**

[etc/quiz-app/src/components/Quiz.vue:65-78](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/components/Quiz.vue#L65-L78) 是判分核心。注意 [第 69–70 行](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/components/Quiz.vue#L69-L70) 的注释「always 3 questions per quiz」与硬编码的 `nextQuestion < 3`：

- 答对且下一题编号 `< 3` → `currentQuestion` 前进；
- 答对且已是第 3 题（`nextQuestion >= 3`）→ `complete = true`，显示恭喜文案；
- 答错 → `error = true`，显示「Sorry, try again」，且**不前进**（用户须答对才能继续）。

> 设计含义：每套测验固定 3 题；答错不会跳过，必须答对才能进入下一题。这是一种「学习导向」而非「评分导向」的交互——错了就重试，直到答对。硬编码的 `3` 是一个隐含契约：如果将来某套测验题数不是 3，这里就会出 bug。

**④ i18n 的真实机制**

这是本模块最需要看懂的地方。`Quiz.vue` 模板用 `questions[currLocale]` 来取当前语言的题目，其中：

[etc/quiz-app/src/components/Quiz.vue:55-57](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/components/Quiz.vue#L55-L57) 定义计算属性 `currLocale`，返回 `this.$root.$i18n.locale`——即**根实例上的全局语言**。

那这个全局语言是谁改的？看 `App.vue`：

[etc/quiz-app/src/App.vue:12-16](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/App.vue#L12-L16) 的语言下拉框用 `v-model="locale"` 绑定到 App 的本地 `locale` 数据；

[etc/quiz-app/src/App.vue:50-54](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/App.vue#L50-L54) 的 `watch` 监听 `locale` 变化，一旦切换就把新值写入 `this.$root.$i18n.locale`。

于是形成一条「数据流」：

```
用户在下拉框选 'es'
   │  (v-model 更新 App.locale)
   ▼
watch 触发 → this.$root.$i18n.locale = 'es'
   │  (全局 i18n 语言变了)
   ▼
Quiz.vue 的计算属性 currLocale 重新求值 → 返回 'es'
   │  (Vue 的响应式：依赖变了，模板自动重渲染)
   ▼
questions['es'] 取出西语题库，页面换成西语题目
```

> **关键洞察**：这里 `vue-i18n` 并没有用它自己的 `$t('key')` 翻译机制，题库是**手动按语言键索引**的（`messages[locale]`）。`vue-i18n` 实际只被借用来当「全局响应式的 locale 变量」。这是真实项目里常见的「混搭」写法——能跑、够用，但不完全符合插件的设计初衷。同时 `App.vue` 的 `created()` 还支持从 URL 查询参数 `?loc=es` 读取初始语言（[第 55–62 行](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/App.vue#L55-L62)），这就是为什么课程链接可以带语言参数直接打开对应语种。

**⑤ 语言包完整度**

[etc/quiz-app/src/assets/translations/es/index.js:1-7](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/es/index.js#L1-L7) 显示西语包**目前只有第 1 课**（`0: es1[0]`），其余 23 课缺失。这意味着切换到 `es` 后，只有第 1 课的测验有西语内容，其它课会因 `questions['es'][n]` 为 `undefined` 而报错或空白——这是一个真实的「翻译未完成」状态，阅读源码时即可发现。

#### 4.3.4 代码实践

**实践目标**：追踪一道具体题目从 URL 到屏幕的完整数据路径（源码阅读型实践，不修改源码）。

**操作步骤**：

1. 在 [etc/quiz-app/src/assets/translations/en/lesson-1.json](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/lesson-1.json) 中找到 `id` 为 `101` 的测验（[第 7–9 行](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/lesson-1.json#L7-L9)），记下它的第 1 道题 `questionText` 和正确选项。
2. 推演：当 URL 是 `/quiz/101` 时，`$route.params.id` 是什么？`Quiz.vue` 里 `this.route` 等于什么？哪一行 `v-if` 让这套测验显示？
3. 推演：用户点了正确选项，`handleAnswerClick(true)` 走哪个分支？`currentQuestion` 如何变化？
4. 推演：用户点了错误选项，走哪个分支？页面上会显示哪段文案（来自 JSON 的哪个字段）？
5.（可选，在**你自己的副本**里）把 `App.vue` 语言下拉框切到 `es`，对照 `es/index.js`，预测哪些课会正常显示、哪些会出问题。

**需要观察的现象**：你能不运行代码、仅靠阅读，准确说出 `/quiz/101` 第 1 题的正确答案文本。

**预期结果**：`/quiz/101` 的第 1 题问的是「19 世纪一位著名的 proto-computer 工程师」，正确答案是 `Charles Babbage`（[lesson-1.json:19-20](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/lesson-1.json#L19-L20)，`isCorrect: true`）。点它会触发 `handleAnswerClick(true)`，`currentQuestion` 从 0 变 1。点错则 `error=true`，显示 `q.error` 即 `"Sorry, try again"`。

**待本地验证**：以上为静态阅读结论；若要确认运行时行为，可结合 4.2.4 的 `npm run serve` 实测。

#### 4.3.5 小练习与答案

**练习 1**：如果有人想新增一门课的测验，最少要改动哪几个文件？

> **答案**：至少要 (1) 在 `src/assets/translations/en/` 新增一个 `lesson-N.json`；(2) 在 [en/index.js](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/index.js) 里 `import` 它并加入键值映射。如果还要西语，同样在 `es/` 下做一遍。这正好是下一讲 [u6-l2 测验内容生成流水线](u6-l2-quiz-generation.md) 要讲的 `qzmkjson.py` 想自动化的事情。

**练习 2**：题目数量被硬编码为 3（`nextQuestion < 3`）。假设某套测验有 5 道题，会发生什么？

> **答案**：答对第 3 题时，`nextQuestion = 3`，`3 < 3` 为假，直接进入 `complete = true` 分支——于是第 4、5 题永远不会显示。用户会被提前告知「完成」，但实际只做了 3 题。这说明 `3` 是一个**隐式契约**，题数必须严格等于 3，否则会出 bug。

**练习 3**：为什么 `Quiz.vue` 用计算属性 `currLocale`（读 `$root.$i18n.locale`）而不是直接读自己的 `locale` 数据？

> **答案**：因为真正切换语言的动作发生在 `App.vue`（它的 `watch` 写 `$root.$i18n.locale`）。`Quiz.vue` 若只读自己的本地 `locale`，就感知不到 App 的切换。通过读取全局 `$root.$i18n.locale` 这个**响应式**的共享状态，Quiz 能在语言一变时自动重新渲染。这是 Vue「响应式 + 单一数据源」思想的体现。

---

## 5. 综合实践

**任务**：完整复述一次「打开测验并答题」的全链路，把本讲三个模块串起来。

请按顺序回答以下问题（可对照源码，但尽量先凭理解作答）：

1. **结构层**：用户在浏览器输入 `http://localhost:8080/quiz/101` 回车。哪份 HTML 文件被加载？里面唯一可挂载的元素是什么？
2. **启动层**：挂载后，`main.js` 第 14 行做了哪三件事（注入了哪两个全局能力、渲染了哪个根组件）？
3. **路由层**：`/quiz/101` 命中了路由表的哪条规则？`:id` 被解析成什么？哪个组件被渲染进 `<router-view>`？
4. **组件层**：`Quiz.vue` 如何用「遍历 + 条件过滤」找到编号 101 的那套测验？题目文本来自哪个 JSON 文件的哪一层？
5. **交互层**：用户答对第 1 题，`handleAnswerClick` 如何推进？答错又如何？答完第 3 题显示什么文案、该文案来自哪里？
6. **i18n 层**：若用户在右上角把语言切到 `es`，数据如何从下拉框流到 `Quiz.vue` 并换出西语题目？为什么切换后只有第 1 课正常？

**进阶（可选，在你自己的 fork/副本中进行，勿改仓库源码）**：

- 在你的副本里给 `es/index.js` 补上第 2 课的西语条目（可临时复制 `lesson-2.json` 到 `es/` 目录并 import），观察首页第 2 课链接在 `es` 下是否不再报错。
- 思考：如果要支持中文（`zh`），按本讲理解，需要新增哪些文件、改哪几处（`translations/`、`App.vue` 下拉框、`translations/index.js`）？

**验收标准**：你能用自己的话，不看讲义，把上述 6 个问题讲给一个没读过源码的人听懂，就说明你真正掌握了 quiz-app 的架构。

## 6. 本讲小结

- quiz-app 是一个 **Vue 2 单页应用**，依赖 `vue`、`vue-router`、`vue-i18n` 三件套，用 `@vue/cli-service` 打包，靠 `package.json` 的 `scripts`（`serve`/`build`/`lint`）驱动开发与构建。
- 启动链路是一条接力：`public/index.html` 的 `<div id="app">` → `main.js` 用 `new Vue({...}).$mount('#app')` 挂载根组件 `App.vue` → `App.vue` 里的 `<router-view>` 根据路由表渲染对应页面。
- 路由表有三条：`/`（首页列表）、`/quiz/:id`（测验，`:id` 为动态参数）、通配 404；`mode: 'history'` 让 URL 干净，但部署时需配 `routes.json` 回退。
- `Quiz.vue` 用两层 `v-for` + `v-if="route == quiz.id"` 过滤出当前测验，`handleAnswerClick` 实现答对前进、答错重试、答完 3 题显示完成；题数 `3` 是硬编码的隐式契约。
- 题库是嵌套对象 `messages[locale][课号].quizzes[套].quiz[题]`，由 `translations/index.js` 与各语言 `index.js` 汇总编号；`en` 完整（24 课）、`es` 目前仅第 1 课。
- 多语言**没有**用 `vue-i18n` 的 `$t()`，而是手动按 `locale` 键索引题库；`vue-i18n` 仅被借用为「全局响应式 locale 状态」，由 `App.vue` 的下拉框与 `watch` 驱动，`Quiz.vue` 通过 `currLocale` 计算属性读取并响应。

## 7. 下一步学习建议

- 下一篇 [u6-l2 测验内容生成流水线](u6-l2-quiz-generation.md) 会讲 `etc/quiz-src/qzmkjson.py` 如何从 `questions-en.txt` 自动生成本讲看到的 `lesson-N.json`——正好补上「题库是怎么来的」这一环，建议紧接着读。
- 想深入 Vue 本身：对照本讲读到的 `new Vue`、`computed`、`watch`、`v-for`、`v-if`、`<router-link>`，去 [Vue 2 官方文档](https://v2.vuejs.org/) 查阅对应章节，把每个概念补全。
- 想理解部署：本讲提到的 `public/routes.json` 与 README 里的 Azure Static Web Apps 流程，可结合 [u6-l5 CI 安全与贡献流程](u6-l5-ci-contributing.md) 进一步了解 GitHub Actions 如何自动构建部署。
- 对多语言机制感兴趣：可对比「标准 `vue-i18n` 的 `$t()` 用法」与本讲的「手动索引」写法，思考各自优劣，并阅读 [u6-l4 多语言翻译机制](u6-l4-translations-i18n.md) 了解仓库层面的 `co-op-translator` 自动翻译。
