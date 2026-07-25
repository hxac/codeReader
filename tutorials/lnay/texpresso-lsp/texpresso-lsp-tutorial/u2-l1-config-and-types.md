# 配置体系与类型定义

## 1. 本讲目标

在前一讲（u1-l4）里，我们已经让连接建好、握手完成，并在 `onInitialize` 里顺带启动了 `texpresso` 子进程。但握手时用到的那些「配置」——主文件叫什么、`texpresso` 装在哪、反向搜索要用哪个编辑器命令——到底是从哪里来、用什么类型描述、又怎么流进服务器的？本讲就把这条「配置链路」彻底讲清楚。

学完后你应当掌握：

- 能区分**初始化选项**（`ServerConfig`）与**工作区设置**（`WorkspaceSettings`）这两类配置，知道它们各自的生命周期。
- 能说清 `root_tex` / `texpresso_path` / `inverse_search` 三个初始化选项的用途，以及 `preview_follow_cursor` 这个工作区设置的作用。
- 能读懂「默认值 + `??` 空值合并」的写法，并意识到其中「整对象替换」与「空串不回落」两个隐藏细节。
- 能判断 `types.ts` 里哪些接口是「在用」的、哪些是「留位但未用」的。

## 2. 前置知识

本讲假设你已经读过 u1-l4，知道：

- LSP 服务器在 `onInitialize` 握手时会收到一个 `params`，其中可以带 `initializationOptions`（初始化选项）。
- `vscode-languageserver` 提供了 `connection.workspace.getConfiguration()` 和 `onDidChangeConfiguration` 两套机制，分别用于**读取**和**监听**工作区设置。
- `server.ts` 顶部用一个对象展开把应用状态塞进了 `connection`：

```ts
const connection = {
    init_options: defaultInitOpts,
    workspace_config: defaultWorkspaceSettings,
    is_texpresso_tonic_running: false,
    ...createConnection(ProposedFeatures.all),
};
```

这两个字段 `init_options` 和 `workspace_config`，就是本讲要讲的「两份配置」的存放处。还需要提醒一个 TypeScript 基础：`??` 是**空值合并运算符**（nullish coalescing），只有当左侧为 `null` 或 `undefined` 时才取右侧；它和 `||`（或）的区别在于 `""`、`0`、`false` 这些「假值但非空」的值不会被 `??` 视为缺失。

## 3. 本讲源码地图

本讲只涉及两个文件，都很短：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/types.ts` | 全项目的类型定义集中地 | `ServerConfig`、`WorkspaceSettings` 两个接口，以及一批「定义了但没被用」的接口 |
| `src/server.ts` | 唯一源码入口、所有运行期逻辑 | `defaultInitOpts` / `defaultWorkspaceSettings` 默认值、`onInitialize` 里的合并、`onInitialized` / `onDidChangeConfiguration` 的热更新 |

一句话定位：`types.ts` 负责「配置长什么样」，`server.ts` 负责「配置默认是什么、怎么合并进来、怎么用出去」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**`ServerConfig` 接口**、**`WorkspaceSettings` 接口**、**默认值与初始化选项合并**。

### 4.1 ServerConfig 接口

#### 4.1.1 概念说明

`ServerConfig` 描述的是**初始化选项**（initialization options）——也就是编辑器在 LSP 握手阶段（`onInitialize`）一次性传给服务器的那份配置。

它有三个字段：

- `root_tex`：主 `.tex` 文件的路径，也就是真正要编译、要预览的那个入口文件（一个 LaTeX 工程可能有成百上千个 `.tex`，但只有一个根）。可相对工作区根目录。
- `texpresso_path`：`texpresso` 可执行文件的路径。如果它已经在 `PATH` 里，直接写 `"texpresso"` 即可。
- `inverse_search`：反向搜索（从 PDF 点击跳回源码）时，服务器要 spawn 的编辑器命令。`command` 是可执行文件名，`arguments` 是参数数组，其中用 `%f` 和 `%l` 作为「文件路径」和「行号」的占位符，运行期会被替换。

初始化选项的关键特性是：**它只在启动时读一次**。想改它，必须重启语言服务器。这和工作区设置（4.2 节）「运行期可热更新」的特性正好相反，这是本讲最重要的一组对比。

#### 4.1.2 核心流程

`ServerConfig` 的数据流是一条「单向、一次性」的链路：

1. 编辑器把初始化选项塞进 `initialize` 请求的 `initializationOptions` 字段。
2. `onInitialize` 回调收到 `params`，从中取出 `params.initializationOptions`。
3. 用 `??` 与默认值逐字段合并，写进 `connection.init_options`。
4. 之后整个程序生命周期里，凡是用到配置的地方（构造子进程、拼反向搜索命令、保存时编译），都读 `connection.init_options`。

伪代码：

```
editor --(initializationOptions)--> onInitialize
onInitialize: connection.init_options.X = user.X ?? default.X
后续代码: 只读 connection.init_options.X
```

注意第 4 步：配置一旦在握手时定下来，后续就是**只读**的，没有再回写 `init_options` 的地方。

#### 4.1.3 源码精读

先看类型定义本身——[src/types.ts:L15-L22](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L15-L22) 定义了 `ServerConfig`：

```ts
export interface ServerConfig {
    root_tex: string;
    texpresso_path: string;
    inverse_search: {
        command: string;
        arguments: string[]; // use %f and %l as placeholders for file and line number
    };
}
```

`inverse_search` 是一个**内联对象类型**（没有单独抽成接口），`arguments` 的注释明确写了 `%f`、`%l` 两个占位符的语义。这个注释是理解后续字符串替换的钥匙。

再看默认值——[src/server.ts:L20-L27](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L20-L27)：

```ts
const defaultInitOpts: ServerConfig = {
    root_tex: "main.tex",
    texpresso_path: "texpresso", // assumes texpresso is in PATH
    inverse_search: {
        command: "zed",
        arguments: ["%f:%l"],
    },
};
```

这里能看到三件事：根文件默认叫 `main.tex`；`texpresso` 默认假设在 `PATH` 里；反向搜索默认用 `zed` 编辑器，参数是 `"%f:%l"`（即 `文件路径:行号` 这种 `file:line` 写法，正是 `zed` 接受的格式）。

最后看配置「用出去」的两个典型消费点。第一个是构造子进程——[src/server.ts:L65-L69](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L65-L69) 把 `texpresso_path` 和 `root_tex` 喂给进程管理器：

```ts
texpressoProcess = new TexpressoProcessManager(
    connection.init_options.texpresso_path,
    ["-json", "-lines"],
    connection.init_options.root_tex
);
```

第二个是反向搜索时拼命令——[src/server.ts:L101-L109](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L101-L109) 读取 `inverse_search` 并做占位符替换：

```ts
const command = connection.init_options.inverse_search.command;
const subs_args =
    connection.init_options.inverse_search.arguments.map(
        (arg) => arg.replace("%f", path).replace("%l", line),
    );
...
spawn(command, subs_args);
```

`%f` 换成 `texpresso` 报上来的文件路径、`%l` 换成行号，然后 `spawn` 出去——这就完成了「PDF → 源码」的跳转。关于这条反向搜索链路的完整讲解，见后续 u3-l2 讲义，本讲只关注「配置如何流到这儿」。

#### 4.1.4 代码实践

**实践目标**：为 `inverse_search` 写一份适用于 VS Code / VSCodium 的配置，理解占位符替换的结果。

**操作步骤**：

1. 回顾默认值 `arguments: ["%f:%l"]`，这是 `zed` 的写法。
2. VS Code / codium 在命令行打开「指定文件 + 指定行」的用法是 `code --goto 文件:行`（codium 把 `code` 换成 `codium`）。
3. 因此把参数拆成两个元素：`"--goto"` 和 `"%f:%l"`。

适合 codium 的初始化选项（示例代码，非项目原有配置）：

```jsonc
{
  "root_tex": "main.tex",
  "texpresso_path": "texpresso",
  "inverse_search": {
    "command": "codium",
    "arguments": ["--goto", "%f:%l"]
  }
}
```

**需要观察的现象**：当你在预览窗口点击 PDF 某一行，服务器日志（`connection.console.log`）会打印一行 `Executing inverse search command: codium --goto /abs/path/main.tex 42`（路径与行号随点击位置变化）。

**预期结果**：codium 把已打开的窗口跳到对应文件的那一行。注意 `%f` 会被替换成 `texpresso` 给的**绝对路径**，`%l` 被替换成行号字符串。

**待本地验证**：VS Code 系编辑器是否会把语言服务器握手时的 `initializationOptions` 透传，取决于具体的 LSP 客户端扩展实现；通用 LSP 客户端是否允许自定义 `inverse_search` 需在你自己的环境里确认。如果日志里压根没出现 `Executing inverse search command`，说明握手阶段这份配置没传进来（回落成了默认的 `zed`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `inverse_search` 要设计成 `command` + `arguments` 数组，而不是直接一个带空格的命令字符串？

**参考答案**：因为最终用的是 `child_process.spawn(command, args)`（见 4.1.3）。`spawn` 要求「可执行文件名」和「参数数组」分开传，它**不会**像 shell 那样对字符串做分词或转义。拆成数组既避免了空格/引号解析的歧义，也让 `%f`/`%l` 占位符能干净地只作用在单个参数上。

**练习 2**：如果把 `inverse_search.command` 设成 `"echo"`、`arguments` 设成 `["%f", "→", "%l"]`，点击 PDF 后会在服务器日志里看到什么？

**参考答案**：会 spawn `echo /abs/path/file.tex → 42`（路径、行号随点击而定）。由于 `echo` 只是把参数打印到自己的 stdout（而这个 stdout 没人读），不会有可见副作用；但服务器在 spawn 前那行 `connection.console.log(...)` 会把替换后的命令原样记录下来，所以你能在 LSP 输出里看到这条命令。这正是一个安全的「干跑」调试手法。

---

### 4.2 WorkspaceSettings 接口

#### 4.2.1 概念说明

`WorkspaceSettings` 描述的是**工作区设置**（workspace settings）——和初始化选项不同，它可以在**运行期**通过编辑器修改，服务器会立刻响应。

目前它只有一个字段：

- `preview_follow_cursor`：布尔值。为 `true` 时，预览窗口会跟随光标所在的行（正向搜索）；为 `false` 时关闭这一行为。

它和 `ServerConfig` 的核心差异可以用一张表概括：

| 维度 | `ServerConfig`（初始化选项） | `WorkspaceSettings`（工作区设置） |
| --- | --- | --- |
| 类型来源 | `types.ts` 的 `ServerConfig` | `types.ts` 的 `WorkspaceSettings` |
| 进入时机 | 握手时（`onInitialize`）一次性传入 | `onInitialized` 拉取，`onDidChangeConfiguration` 监听变更 |
| 能否运行期改 | 不能，需重启服务器 | 能，热更新 |
| 存放位置 | `connection.init_options` | `connection.workspace_config` |
| 传输通道 | `initialize` 请求的 `initializationOptions` | `workspace/didChangeConfiguration` 通知 |

记住这张表，本讲的「区分两类配置」目标就达成了。

#### 4.2.2 核心流程

工作区设置是一条「双向、可热更新」的链路，分三段：

1. **初始化拉取**：握手完成后触发 `onInitialized`，主动调 `connection.workspace.getConfiguration()` 读一次当前设置，写进 `connection.workspace_config`。
2. **变更监听**：之后用户在编辑器里改设置，编辑器会发 `workspace/didChangeConfiguration` 通知，触发 `onDidChangeConfiguration`，服务器更新 `connection.workspace_config`。
3. **读取消费**：真正用到这个开关的地方（`onDocumentHighlight`，即「光标移动」事件），每次都现读 `connection.workspace_config.preview_follow_cursor`。

伪代码：

```
onInitialized:        workspace_config.X = getConfiguration().X ?? default.X   // 读一次
onDidChangeConfiguration: workspace_config.X = change.settings.X                 // 后续热更新
onDocumentHighlight:  if (workspace_config.X) { ... }                           // 每次现读
```

关键点：消费端**不缓存**，每次都读最新的 `workspace_config`，所以设置改了立刻生效。

#### 4.2.3 源码精读

类型定义——[src/types.ts:L25-L27](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L25-L27)，只有一个布尔字段：

```ts
export interface WorkspaceSettings {
    preview_follow_cursor: boolean;
}
```

默认值——[src/server.ts:L29-L31](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L29-L31)，默认开启跟随：

```ts
const defaultWorkspaceSettings: WorkspaceSettings = {
    preview_follow_cursor: true,
};
```

第一段「初始化拉取」——[src/server.ts:L134-L150](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L134-L150)：

```ts
connection.onInitialized(async () => {
    try {
        const config = await connection.workspace.getConfiguration();
        if (config) {
            connection.workspace_config.preview_follow_cursor =
                config.preview_follow_cursor ??
                defaultWorkspaceSettings.preview_follow_cursor;
            ...
        }
    } catch (error) { ... }
});
```

注意这里同样用了 `??`：编辑器没给值就回落到默认 `true`。

第二段「变更监听」——[src/server.ts:L153-L167](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L153-L167)：

```ts
connection.onDidChangeConfiguration(async (change) => {
    ...
    const config = change.settings;
    if (config && config.preview_follow_cursor !== undefined) {
        connection.workspace_config.preview_follow_cursor =
            config.preview_follow_cursor;
        ...
    }
    ...
});
```

这里的守卫从 `??` 换成了 `!== undefined` 显式判断——语义上和 `??` 接近（都允许 `false` 通过），但写法不同。这是真实代码里常见的「同一意图、两种写法」，读代码时要适应。

第三段「读取消费」——[src/server.ts:L232-L237](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L232-L237)，在 `onDocumentHighlight` 里现读：

```ts
if (!connection.workspace_config.preview_follow_cursor) {
    connection.console.log(
        "Document highlight ignored: preview_follow_cursor is disabled",
    );
    return [];
}
```

关掉开关后，正向搜索命令（`synctex-forward`）就不会发了。这就是「运行期改设置 → 立刻生效」的落点。完整正向搜索机制见 u3-l2。

#### 4.2.4 代码实践

**实践目标**：验证 `preview_follow_cursor` 的运行期热更新。

**操作步骤**：

1. 按默认值（`true`）启动服务器，打开一个 `.tex` 文件，在编辑器里移动光标。
2. 观察 LSP 输出：应能看到 `Document highlight request: ...` 和 `Sent synctex-forward command: ...` 日志，预览窗口跟随光标。
3. **不重启服务器**，在编辑器设置里把 `texpresso.preview_follow_cursor` 改成 `false`。
4. 继续移动光标。

**需要观察的现象**：第 3 步之后，编辑器配置变更会触发 `onDidChangeConfiguration`，服务器日志出现 `Configuration changed` 和 `Updated workspace settings: preview_follow_cursor = false`。之后移动光标，日志变成 `Document highlight ignored: preview_follow_cursor is disabled`，且不再有 `synctex-forward`。

**预期结果**：行为在运行期即时翻转，无需重启——这正是工作区设置区别于初始化选项的核心价值。

**待本地验证**：`connection.workspace.getConfiguration()` 在你的 LSP 客户端下实际返回的对象结构（是否带 `texpresso.` 命名空间前缀）需本地确认。代码里直接读 `config.preview_follow_cursor`，而 README 把设置名写成 `texpresso.preview_follow_cursor`，这两者之间的命名空间关系建议在本地日志里核实——见 4.3.5 练习 3。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `preview_follow_cursor` 被放进工作区设置，而不是初始化选项？

**参考答案**：因为它是一个用户在使用过程中**可能想随手开关**的偏好（写代码时跟随、浏览输出时关掉）。放进工作区设置就能运行期热更新；若放进初始化选项，每次切换都要重启服务器，体验很差。配置的「易变性」应该决定它归属哪一类。

**练习 2**：`onDidChangeConfiguration` 里用的是 `!== undefined`，而 `onInitialized` 里用的是 `??`。对于 `preview_follow_cursor` 这个布尔字段，这两种写法在「编辑器传了 `false`」时行为一样吗？

**参考答案**：一样。`false ?? default` 会保留 `false`（因为 `false` 既不是 `null` 也不是 `undefined`）；`false !== undefined` 也为真、保留 `false`。两种写法都正确地允许 `false` 通过、不会被默认值 `true` 覆盖。这就是为什么这里不能用 `||`——`false || true` 会错误地变成 `true`。

---

### 4.3 默认值与初始化选项合并

#### 4.3.1 概念说明

前两节看到了「默认值 + 用户值」的合并模式反复出现。这一节把它抽象成一个统一规则，并指出两个容易踩坑的细节。

合并的核心运算符是 `??`（空值合并）。形式化地，单字段合并函数定义为：

\[
\text{merge}(u, d) =
\begin{cases}
u & u \neq \text{null} \;\land\; u \neq \text{undefined} \\
d & \text{otherwise}
\end{cases}
\]

其中 \(u\) 是用户传入值，\(d\) 是默认值。也就是说：**用户给了（且不是 null/undefined）就用用户的，否则回落默认**。

但这个「给了」的判定有两个层面，对应两个隐藏细节：

- **细节一：标量字段是「逐字段」合并。** `root_tex`、`texpresso_path` 各自独立判断。于是 `root_tex: ""`（空串）会被当作「给了」而保留，**不会**回落到 `"main.tex"`——因为空串不是 null/undefined。这点和直觉（「空串等于没填」）相反。
- **细节二：`inverse_search` 是「整对象」合并，不是逐字段。** 代码写的是 `init_params.inverse_search ?? defaultInitOpts.inverse_search`，把整个对象当成一个值来判空。于是用户只要传了 `inverse_search` 对象，哪怕里面**缺了 `arguments`**，默认值也不会来补——那个 `arguments` 字段就会是 `undefined`。

#### 4.3.2 核心流程

把三处合并放在一起看，统一流程是：

1. 顶部声明 `defaultInitOpts` / `defaultWorkspaceSettings` 两份默认值。
2. `connection.init_options` / `connection.workspace_config` 在创建时就指向这两份默认值（基线）。
3. 握手时 `onInitialize` 用 `??` 把 `initializationOptions` 的标量字段逐个覆盖到 `init_options`。
4. `onInitialized` / `onDidChangeConfiguration` 用 `??` 或 `!== undefined` 维护 `workspace_config`。

合并策略一览：

| 字段 | 合并粒度 | 运算符 | 空串 `""` 会回落默认？ |
| --- | --- | --- | --- |
| `root_tex` | 逐字段 | `??` | 否（保留 `""`） |
| `texpresso_path` | 逐字段 | `??` | 否（保留 `""`） |
| `inverse_search` | 整对象 | `??` | — （对象判空，部分缺失字段不补） |
| `preview_follow_cursor` | 逐字段 | `??` / `!== undefined` | — （布尔） |

#### 4.3.3 源码精读

默认值与存放处——[src/server.ts:L20-L38](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L20-L38) 一次性声明两份默认值并把它们装进 `connection`：

```ts
const defaultInitOpts: ServerConfig = { ... };
const defaultWorkspaceSettings: WorkspaceSettings = { ... };

const connection = {
    init_options: defaultInitOpts,        // 初始化选项的存放处，初值=默认
    workspace_config: defaultWorkspaceSettings, // 工作区设置的存放处，初值=默认
    is_texpresso_tonic_running: false,
    ...createConnection(ProposedFeatures.all),
};
```

注意：`init_options` 一开始就**指向** `defaultInitOpts` 对象。下面合并时是「改写 `init_options` 的字段」而不是「换一个对象」。

握手时的逐字段合并——[src/server.ts:L49-L62](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L49-L62)：

```ts
let init_params;
if ((init_params = params.initializationOptions)) {
    connection.init_options.root_tex =
        init_params.root_tex ?? defaultInitOpts.root_tex;
    connection.init_options.texpresso_path =
        init_params.texpresso_path ?? defaultInitOpts.texpresso_path;
    connection.init_options.inverse_search =
        init_params.inverse_search ?? defaultInitOpts.inverse_search;
}
```

读这段代码要注意三件事：

1. 外层 `if ((init_params = params.initializationOptions))` 是「赋值 + 真值判断」二合一：只有编辑器**传了** `initializationOptions` 这个对象，才进入合并；否则整个 `connection.init_options` 就保持等于默认值。
2. 前两行是**标量逐字段**合并，符合直觉。
3. 第三行 `inverse_search` 是**整对象**合并——这正是 4.3.1 细节二的来源。如果用户传 `inverse_search: { command: "code" }`（漏了 `arguments`），那么 `connection.init_options.inverse_search.arguments` 就是 `undefined`；等反向搜索触发、代码走到 4.1.3 那段 `.arguments.map(...)` 时，会对 `undefined` 调 `.map` 而抛错。这是一个真实存在的潜在缺陷，值得作为练习深究。

#### 4.3.4 代码实践（本讲主实践）

这是本讲规格指定的综合小实践，分两部分。

**Part A：找出未被实际使用的接口。**

**操作步骤**：

1. 打开 [src/types.ts:L1-L48](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/types.ts#L1-L48)，列出全部导出接口与枚举。
2. 在 `src/` 下搜索每个接口的引用（你已经在 `server.ts` 顶部看到 `import { ServerConfig, WorkspaceSettings } from "./types"`，只有这两个被引入）。
3. 对比「定义」与「引用」两个集合。

**需要观察的现象与预期结果**：

| 类型 | 是否被 `server.ts` 实际使用 | 说明 |
| --- | --- | --- |
| `ServerConfig` | ✅ 在用 | 作为 `defaultInitOpts` 的类型、`init_options` 的类型 |
| `WorkspaceSettings` | ✅ 在用 | 作为 `defaultWorkspaceSettings`、`workspace_config` 的类型 |
| `CustomDiagnostic` | ❌ 未使用 | 仅被下面三个未用类型引用，自身也没被 `server.ts` 引用 |
| `DiagnosticTag` | ❌ 未使用 | 仅服务于 `CustomDiagnostic.tags` |
| `CustomRule` | ❌ 未使用 | 描述「自定义诊断规则」，无任何实现消费它 |
| `AnalysisResult` | ❌ 未使用 | 描述「文档分析结果」，无消费方 |
| `DocumentStatistics` | ❌ 未使用 | 描述「文档统计」，无消费方 |

结论：`CustomDiagnostic` / `DiagnosticTag` / `CustomRule` / `AnalysisResult` / `DocumentStatistics` 这五个类型构成了一个**「自定义诊断/分析」功能的脚手架**——类型先写好了，但对应的功能（在文档上做自定义 lint、产出 diagnostics、统计行数等）尚未实现。它们是典型的「留位代码」（scaffolding），提示了项目未来可能的方向，也属于 u3-l4 会讨论的技术债。

**Part B：为 `inverse_search` 写一份编辑器配置。**

见 4.1.4，已为 codium 给出 `["--goto", "%f:%l"]` 的写法。请仿照为你自己常用的编辑器（如 VS Code 用 `code`、Neovim 用 `nvim`、Emacs 用 `emacsclient`）写一份 `command` + `arguments`，并口算一遍 `%f`/`%l` 替换后的最终命令长什么样。

#### 4.3.5 小练习与答案

**练习 1**：用户在初始化选项里传了 `root_tex: ""`（空串）。最终 `connection.init_options.root_tex` 是什么？之后保存触发的编译会用哪个文件？

**参考答案**：是空串 `""`。因为 `"" ?? default` 的结果是 `""`（空串不是 null/undefined，`??` 保留它）。于是在 [src/server.ts:L180-L181](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L180-L181)，`path = connection.init_options.root_tex` 会是 `""`，`spawn(texpresso_tonic_path, ["-k", ""])` 会拿空文件名去编译，几乎必然失败。这正是 4.3.1 细节一描述的「空串不回落」陷阱——如果作者的意图是「空串视同未填」，应该用 `||` 而非 `??`，或在合并前显式判空串。

**练习 2**：用户在初始化选项里传了 `inverse_search: { command: "code" }`（漏了 `arguments`）。握手能成功吗？什么时候会出问题？

**参考答案**：握手**能**成功。因为 `init_params.inverse_search` 是个真值对象，`??` 直接采用它，`arguments` 字段缺失即为 `undefined`，但这在握手阶段不会被察觉。问题会推迟到**第一次反向搜索**触发时：代码在 [src/server.ts:L102-L105](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L102-L105) 执行 `connection.init_options.inverse_search.arguments.map(...)`，对 `undefined` 调 `.map` 会抛 `TypeError`。这个错误会被 `synctex` 的事件回调吞掉（该回调没有 try/catch），表现为反向搜索「点了没反应」。修复思路：要么对 `inverse_search` 也做逐字段合并，要么在消费前判 `arguments` 是否存在。

**练习 3（待本地验证）**：README 写设置名是 `texpresso.preview_follow_cursor`（带命名空间），但 [src/server.ts:L136-L140](https://github.com/lnay/texpresso-lsp/blob/c13ec89e84758ba32fe6d2e8ccfd402abb8c311d/src/server.ts#L136-L140) 里读的是 `config.preview_follow_cursor`（不带前缀）。这两者能对上吗？

**参考答案（思路）**：`connection.workspace.getConfiguration()` 不带参数调用时返回的对象结构，取决于具体 LSP 客户端如何组织设置。如果客户端把 `texpresso.*` 收敛到一个子对象下，那么正确读法应是 `config.texpresso?.preview_follow_cursor`，而代码直接读 `config.preview_follow_cursor` 可能始终拿到 `undefined`、从而永远回落默认值 `true`——意味着用户在编辑器里改成 `false` 可能不生效。这是一种「命名空间不匹配」的潜在缺陷。但由于 `getConfiguration()` 的返回结构依赖客户端，最终结论需在本地日志里验证（看 `Initialized workspace settings: preview_follow_cursor = ...` 打出的值是否随设置变化）。

## 5. 综合实践

把本讲三块知识串起来，做一个「配置体检」小任务。

**任务**：假设你接手了这个项目，要给一位新用户写一份「最小可用配置清单」。请完成：

1. **画配置数据流图**：画出从「编辑器」到「`connection.init_options` / `connection.workspace_config`」再到「消费点」（子进程构造、反向搜索、保存编译、正向搜索）的完整数据流，标注每一处用的是初始化选项还是工作区设置。
2. **写两份配置**：分别为「zed 用户」和「VS Code/codium 用户」写出初始化选项 JSON 与工作区设置 JSON，并说明哪些字段可省略、省略后回落到什么默认值。
3. **找隐患**：基于本讲练习 1、2、3，列出当前合并逻辑里你发现的三个潜在问题（空串不回落、`inverse_search` 整对象替换、命名空间可能不匹配），并为每一个写一句「会如何表现 + 该如何验证」。
4. **清点死代码**：列出 `types.ts` 里未被使用的接口，并给出一句话判断——它们是该删，还是该保留为未来功能的占位？

这个任务不需要你改源码（本讲禁止改源码），只产出一份分析文档即可。它的价值在于：你能否用本讲的「两类配置 + 合并规则」这套语言，去解释一个真实项目里所有与配置有关的行为和隐患。

## 6. 本讲小结

- 项目有**两类配置**：初始化选项（`ServerConfig`，握手时一次性传入、不可运行期改）和工作区设置（`WorkspaceSettings`，运行期热更新）。
- `ServerConfig` 的三个字段：`root_tex`（主文件）、`texpresso_path`（可执行文件路径）、`inverse_search`（反向搜索命令，用 `%f`/`%l` 占位符）。
- `WorkspaceSettings` 目前只有一个 `preview_follow_cursor`，经 `onInitialized` 拉取、`onDidChangeConfiguration` 监听、`onDocumentHighlight` 现读。
- 合并统一用 `??`（空值合并），意为「用户给了且非空就用用户的，否则回落默认」。
- 两个隐藏细节：标量字段「空串不回落」；`inverse_search` 是「整对象合并」，部分缺失字段不会被默认值补齐，是一个潜在缺陷。
- `types.ts` 中 `CustomDiagnostic` / `DiagnosticTag` / `CustomRule` / `AnalysisResult` / `DocumentStatistics` 五个类型是未实际使用的脚手架，预示了一个尚未实现的「自定义诊断/分析」功能方向。

## 7. 下一步学习建议

配置已经流进了 `connection.init_options`，握手阶段还用它做了一件关键的事——**启动 `texpresso` 子进程**（见 4.1.3 那段 `new TexpressoProcessManager(...)`）。这个进程管理器是怎么 spawn 子进程、怎么管理生命周期的？这正是下一讲 **u2-l2 进程管理器 TexpressoProcessManager** 的主题，我们会精读 `src/process-manager.ts`。

如果你对配置链路里的隐患（尤其是练习 2、3）感兴趣，也可以先跳到 **u3-l4 架构取舍与二次开发**，那里会系统讨论这类技术债。但建议按顺序先读完 u2-l2、u2-l3、u2-l4，把「配置 → 进程 → 协议 → 文档同步」这条主链路打通，再回头看架构问题会更有全局感。
