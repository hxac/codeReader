# u11-l3 Flutter 前端如何对接 Go 核心

## 1. 本讲目标

前面 u8-l3、u8-l4 站在 Go 侧看了原生桥接（`bind/desktop`、`bind/mobile`、`pkg/rest/invoke.go`）与任务事件管道（`pkg/api/events.go`）。本讲调转视角，站到 `ui/flutter` 一侧，回答以下问题：

1. Flutter 应用启动时如何把 Go 核心拉起来（启动引导 `libgopeed_boot`）。
2. 业务代码如何用同一套 `api.dart` 调用在桌面 FFI、移动 gomobile、Web REST 三种传输上工作。
3. 任务事件如何从 Go 回调变成 Flutter 的 `Stream<TaskEvent>`。
4. d69874e 引入的「统一子表面路由」：`AppRouter.openWindow` / `AppWindowPage` / `AppWindowContent` 如何把桌面子窗口、Web 应用内模态、移动全页三种呈现形态收敛到一个入口。
5. 窗口能力 RPC（`WindowMethodChannel`）的边界，以及 b0d4522 的 macOS 子窗口引擎释放与 `_removeClosedSubscribers` 订阅清理机制。
6. 任务分组批量下载（u4-l6 的 `CreateTaskBatch`）在前端的呈现：`PendingDownload` 预览模型、`planDownloads` 分组规划与 `DownloadSubmission` 提交。

学完本讲，你应该能在 Flutter 代码里独立定位「创建任务、批量/分组创建、订阅事件、读取配置」四条调用链，并解释每个文件为什么出现在链路上的那个位置。

## 2. 前置知识

- **FFI（Foreign Function Interface）**：Dart 直接调用 C ABI 函数的机制。桌面端 Go 编译成 c-shared 动态库，Flutter 通过 `dart:ffi` 加载并调用。
- **gomobile**：Go 官方的移动端绑定工具，把 Go 代码打包成 Android AAR / iOS XCFramework，Flutter 侧经 `MethodChannel` 调用。
- **MethodChannel**：Flutter 与原生平台（Kotlin/Swift）之间双向消息通道。既可 Flutter→原生（`invokeMethod`），也可原生→Flutter（反向推送，本讲的事件回运用到）。
- **条件导入（conditional import）**：Dart 的 `if (dart.library.html) ... if (dart.library.io) ...` 语法，编译期按平台选择不同实现文件，是 Flutter 跨平台分叉的惯用手段。
- **Riverpod**：Flutter 的状态管理与依赖注入框架。本讲中控制器（controller）都通过 `ref.read(provider)` 获取服务。
- **go_router**：Flutter 的声明式路由库。`GoRoute` 声明路径，`router.push`/`router.go` 导航。本讲的 Web 模态就是一个挂在根 Navigator 上的路由页面。
- **子表面（child surface）**：本讲对「主窗口之外承载某个功能的界面」的统称——桌面上是独立子窗口，Web 上是应用内模态，移动端是全页。这是 d69874e 统一后的概念。
- **desktop_multi_window**：Flutter 桌面多窗口插件，主窗口与子窗口各跑一个独立的 Flutter engine（各自的 Dart isolate），之间只能靠 `WindowMethodChannel` 通信。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ui/flutter/docs/backend-transport-architecture.md` | Go–Flutter 进程内 API 架构决策文档：三端传输策略表、Dispatcher、事件设计 |
| `ui/flutter/docs/window-capability-rpc.md` | 主窗口与子窗口的能力 RPC 契约，第 11 节「Opening Child Surfaces」为 d69874e 新增 |
| `ui/flutter/lib/core/libgopeed_boot.dart` | 启动引导抽象类，条件导入分三端实现 |
| `ui/flutter/lib/core/entry/libgopeed_boot_native.dart` | 原生端启动实现：桌面选 FFI、移动选 MethodChannel |
| `ui/flutter/lib/core/entry/libgopeed_boot_browser.dart` | Web 端空实现（start 无操作、invoke 抛异常） |
| `ui/flutter/lib/core/common/libgopeed_channel.dart` | 移动端 gomobile 的 MethodChannel 适配 |
| `ui/flutter/lib/core/ffi/libgopeed_worker.dart` | 桌面 FFI 独立 isolate worker，持有动态库与回调 |
| `ui/flutter/lib/api/api.dart` | 业务 API 门面：`createTask`、`createTaskBatch`、`getConfig` 等顶层函数 |
| `ui/flutter/lib/core/network/gopeed/gopeed_transport.dart` | `GopeedTransport` 接口与三端工厂 |
| `ui/flutter/lib/core/capabilities/gopeed_capability.dart` | `GopeedMethods`（RPC 方法目录）与 `GopeedService`（类型化门面） |
| `ui/flutter/lib/api/model/create_task_batch.dart` | `CreateTaskBatch` / `CreateTaskBatchItem` / `CreateTaskBatchResult` 模型 |
| `ui/flutter/lib/features/tasks/application/pending_download.dart` | 下载预览领域模型：resolve、文件选择、批量项生成、分组规划 |
| `ui/flutter/lib/app/application/app_runtime_controller.dart` | 主窗口运行时控制器：启动 Go、订阅事件、初始化 api 传输 |
| `ui/flutter/lib/app/application/app_notification_controller.dart` | 桌面通知控制器：消费 `taskEvents` 流 |
| `ui/flutter/lib/app/router/app_router.dart` | 路由与统一子表面入口 `openWindow`/`openCreateTask` |
| `ui/flutter/lib/app/router/app_window_page.dart` | Web/桌面回退用的应用内模态 `Page`（DialogRoute） |
| `ui/flutter/lib/app/router/app_window_content.dart` | 子表面目的地注册表：路径映射与内容构建 |
| `ui/flutter/lib/core/window/app_window_launcher.dart` | 桌面子窗口启动器，失败时回退应用内模态 |
| `ui/flutter/lib/core/window/app_window_payload.dart` | 子表面启动载荷（可序列化字符串） |
| `ui/flutter/lib/core/window/window_capability_transport.dart` | 窗口能力 RPC：协议常量、宿主、子窗口会话、订阅清理 |
| `ui/flutter/lib/app/child_window_app.dart` | 子窗口的根 Widget：外观快照驱动的主题与页面 |
| `ui/flutter/macos/Runner/MainFlutterWindow.swift` | macOS 主窗口，b0d4522 在此释放关闭的子窗口 engine |
| `ui/flutter/test/app/router/app_window_navigation_test.dart` | 统一子表面路由的 351 行导航测试（d69874e 新增） |
| `ui/flutter/test/core/window/window_capability_lifecycle_test.dart` | 订阅清理回归测试（b0d4522 新增） |

## 4. 核心概念与源码讲解

本讲的最小模块共五个：启动引导、api 调用层、事件订阅、统一子表面路由与 Web 模态、窗口能力传输与 macOS 引擎释放。

### 4.1 启动引导：libgopeed_boot 的三端分叉

#### 4.1.1 概念说明

Go 核心不会自己跑起来，它被编译成动态库（桌面）或 AAR/XCFramework（移动）后，必须由 Flutter 在进程内显式启动。启动引导层要解决三个问题：

1. **三端差异**：桌面用 FFI 直连；移动用 MethodChannel + gomobile；Web 根本没有 Go 进程（页面由远端 cmd/web 服务，Flutter 只是 REST 客户端）。
2. **生命周期唯一性**：Go Runtime 全进程只能有一份，主窗口是唯一 owner，重复初始化（如热重载）必须安全。
3. **API 服务器生命周期独立**：原生端默认不监听 HTTP，可选开启；这些状态查询/启停也要走引导层。

架构文档把三端策略写成了一张表（桌面 Dart FFI / 移动 MethodChannel+gomobile / Web REST），这是整个前端通信选型的出发点。

#### 4.1.2 核心流程

```text
应用启动（主窗口）
  → AppRuntimeController 启动流程
    → LibgopeedBoot.instance.start(StartConfig)      # 条件导入选实现
        ├─ 桌面: LibgopeedFFi（独立 isolate 加载动态库）
        ├─ 移动: LibgopeedChannel（MethodChannel → gomobile）
        └─ Web:  LibgopeedBootBrowser（直接返回 0，什么都不做）
    → LibgopeedBoot.instance.subscribeTaskEvents({done, error})
    → api.init(network, address, apiToken)            # 初始化业务传输层
    → 读配置、恢复窗口状态 …
```

关键点：`start()` 在原生实现里会强制 `storage = 'bolt'`、`refreshInterval = 0`（进度刷新由 Flutter 拉取轮询接管，见 u4-l3），移动端额外指定临时目录。

#### 4.1.3 源码精读

先看抽象类与条件导入——注意文件顶部的 `if (dart.library.html)` / `if (dart.library.io)`，这是编译期分叉，Web 与原生各自链接到不同实现：

[ui/flutter/lib/core/libgopeed_boot.dart:4-6](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/libgopeed_boot.dart#L4-L6) 按 Dart 可用标准库在编译期选择 browser / native / stub 实现；

[ui/flutter/lib/core/libgopeed_boot.dart:8-35](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/libgopeed_boot.dart#L8-L35) 定义抽象接口：`start/stop`、API 服务器四个操作、`invoke(method, path, query, body)`、`taskEvents` 流与 `subscribeTaskEvents`。注意 `invoke` 的参数正是 u8-l1 讲过的 Dispatcher 归一化请求格式。

原生实现的构造函数按平台二选一：

[ui/flutter/lib/core/entry/libgopeed_boot_native.dart:16-25](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/entry/libgopeed_boot_native.dart#L16-L25) 桌面选 `LibgopeedFFi`、移动选 `LibgopeedChannel`；

[ui/flutter/lib/core/entry/libgopeed_boot_native.dart:27-36](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/entry/libgopeed_boot_native.dart#L27-L36) `start` 强制 bolt 存储、固定存储目录、`refreshInterval = 0`，移动端补临时目录。

Web 实现是「空壳」——这正是架构文档说的「Web 只是 REST 客户端」：

[ui/flutter/lib/core/entry/libgopeed_boot_browser.dart:11-41](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/entry/libgopeed_boot_browser.dart#L11-L41) `start` 返回 0，`invoke` 抛 `UnsupportedError`，`taskEvents` 是空流，API 服务器操作返回「未启用」的固定结果。

移动端 MethodChannel 适配——注意构造函数里的反向消息注册，这是事件回运的入口：

[ui/flutter/lib/core/common/libgopeed_channel.dart:16-22](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/common/libgopeed_channel.dart#L16-L22) 注册 `taskEvent` 反向回调，解码 JSON 后灌入广播流；

[ui/flutter/lib/core/common/libgopeed_channel.dart:52-63](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/common/libgopeed_channel.dart#L52-L63) `invoke` 携带递增 `requestID` 发起异步调用（对应 u8-l3 的 gomobile `InvokeAsync`）。

桌面 FFI 走独立 isolate worker，主 isolate 通过消息端口通信：

[ui/flutter/lib/core/ffi/libgopeed_worker.dart:58-66](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/ffi/libgopeed_worker.dart#L58-L66) `invoke` 与 `subscribeTaskEvents` 都封装成带请求 ID 的消息；

[ui/flutter/lib/core/ffi/libgopeed_worker.dart:81-100](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/ffi/libgopeed_worker.dart#L81-L100) worker 回复按类型分发：`response` 完成 pending Completer，`taskEvent` 灌入事件流。

最后是主窗口里的实际启动点：

[ui/flutter/lib/app/application/app_runtime_controller.dart:137-161](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/application/app_runtime_controller.dart#L137-L161) 启动序列：`LibgopeedBoot.instance.start` → `subscribeTaskEvents({done, error})` → 读 API 服务器状态 → `api.init(...)`。Web 分支传入页面同源地址，原生分支传入本地地址；注释说明 Go 侧 `rest.Start` 幂等保证热重载安全。

#### 4.1.4 代码实践

1. **实践目标**：不运行程序，仅凭源码画出「三端启动序列对比图」。
2. **操作步骤**：
   - 读 [backend-transport-architecture.md:19-25](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/docs/backend-transport-architecture.md#L19-L25) 的策略表。
   - 对照 `libgopeed_boot_native.dart` 与 `libgopeed_boot_browser.dart`，分别写出三端 `start()` 的实际效果（桌面：FFI 加载动态库；移动：MethodChannel 触发 gomobile `Start`；Web：无操作返回 0）。
   - 用 `grep -rn "LibgopeedBoot.instance.start" ui/flutter/lib` 找出唯一调用点，确认「主窗口是唯一 owner」。
3. **需要观察的现象**：纸上图中 Web 列应当没有任何本地 Go 调用，全部走 HTTP。
4. **预期结果**：三行对比表（桌面/移动/Web × start/invoke/taskEvents），与架构文档表格一致。
5. 本实践为源码阅读型，无需运行，结论可直接核对源码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `libgopeed_boot_browser.dart` 的 `invoke` 要抛 `UnsupportedError`，而不是返回空字符串？
**答案**：Web 端不存在本地 Go 进程，「进程内调用」这个能力本身不成立。抛异常能让误用（例如某组件在 Web 上调用了 `LibgopeedBoot.instance.invoke`）在开发期立刻暴露，而返回空串会把错误吞掉、延迟到 JSON 解析处才以更难排查的方式失败。

**练习 2**：`AppRuntimeController` 启动时为什么把 `refreshInterval` 设为 0（见 `libgopeed_boot_native.dart` 的 `start`）？
**答案**：`refreshInterval` 是 Go 侧 watch 循环的进度刷新/事件周期（u4-l3）。设为 0 表示关闭高频进度事件推送，Flutter 改用自己的轮询节奏拉取任务列表；结合架构文档第 6 节，原生端只订阅 done/error 两个低频终态事件，避免无谓的高频回调。

### 4.2 api 调用层：一套业务方法，三种传输

#### 4.2.1 概念说明

`lib/api/api.dart` 是历史遗留的门面层：一组顶层函数（`resolve`、`createTask`、`getConfig`…），内部把「路径 + 方法 + 参数」交给一个 `GopeedTransport`。传输接口刻意只做搬运：

- **Web**：`RestTransport`（Dio HTTP）。
- **桌面**：`NativeGopeedTransport` → FFI isolate worker 的 `invoke`。
- **移动**：`NativeGopeedTransport` → `LibgopeedBoot`（MethodChannel）。

架构文档 7.2 节的规则是「传输层禁止暴露业务方法」——`createTask()` 这类语义只写在门面里一次，新 API 不需要改任何传输实现。另一套并行体系是窗口能力层的 `GopeedService`（4.5 节）：子窗口不能 import `api.dart`，主窗口把同一批能力包装成 RPC 方法供子窗口远程调用。

#### 4.2.2 核心流程

```text
业务代码 → api.createTaskBatch(batch)
  → _request('api/v1/tasks/batch', POST, data)
    → initDefault()（惰性建传输）
    → transport.request(...)
        ├─ Web:   Dio 发 HTTP
        └─ 原生:  拆 path/query → jsonEncode(body)
                   → worker.invoke(method, path, query, body)   # 桌面 FFI
                   → LibgopeedBoot.instance.invoke(...)          # 移动
                   → Go Dispatcher 执行同一 RouteSpec（u8-l1）
  → Result<T> 信封解析，code != 0 抛异常
```

#### 4.2.3 源码精读

门面层持有一个模块级传输单例，并惰性初始化默认值：

[ui/flutter/lib/api/api.dart:29-50](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/api/api.dart#L29-L50) `_transport` 单例、`init`/`initDefault` 构造传输、`_request` 统一出口——任何门面函数最终都汇聚到这一行 `transport.request`。

创建任务与批量创建是本讲重点关注的两个调用：

[ui/flutter/lib/api/api.dart:84-93](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/api/api.dart#L84-L93) `createTask` POST `api/v1/tasks` 返回任务 ID 字符串；`createTaskBatch` POST `api/v1/tasks/batch`，用 `CreateTaskBatchResult.fromJson` 解码出 `taskIds` 与可选 `parentId`——正是 u4-l6 讲的批量响应新结构。

读配置与任务列表：

[ui/flutter/lib/api/api.dart:99-105](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/api/api.dart#L99-L105) `getTasks` 把状态列表拼成重复的 `status=` 查询参数；

[ui/flutter/lib/api/api.dart:149-158](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/api/api.dart#L149-L158) `getConfig`/`putConfig` 读写 `DownloaderStoreConfig`（u2-l3 的全局配置体系）。

传输接口与三端工厂：

[ui/flutter/lib/core/network/gopeed/gopeed_transport.dart:59-65](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/network/gopeed/gopeed_transport.dart#L59-L65) `GopeedTransport` 接口只有 `request` 和 `join` 两个方法，没有任何业务语义；

[ui/flutter/lib/core/network/gopeed/gopeed_transport.dart:27-31](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/network/gopeed/gopeed_transport.dart#L27-L31) Web 地址决策：debug 构建连本地 9999 端口，release 构建用页面自身 origin（同源 REST）。

原生传输的实现——把门面请求翻译成 Dispatcher 的归一化四元组：

[ui/flutter/lib/core/network/gopeed/entry/gopeed_transport_native.dart:10-37](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/network/gopeed/entry/gopeed_transport_native.dart#L10-L37) 桌面有 FFI worker 时直接 `worker.invoke`；否则（移动）落到 `LibgopeedBoot.instance.invoke`。`_RequestParts` 负责把 path 中的查询串与 `queryParameters` 合并编码——Go 侧 Dispatcher 期望 query 是一个原始字符串（u8-l1）。

窗口能力侧的类型化门面（子窗口与主窗口共用）：

[ui/flutter/lib/core/capabilities/gopeed_capability.dart:15-20](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/capabilities/gopeed_capability.dart#L15-L20) `GopeedMethods` 把每个操作声明成稳定的 `RpcMethod<P,R>` 协议名（如 `gopeed.task.createBatch`），禁止在页面里散落字符串；

[ui/flutter/lib/core/capabilities/gopeed_capability.dart:43-57](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/capabilities/gopeed_capability.dart#L43-L57) `GopeedService` 提供类型化方法，内部经注入的 `CapabilityInvoker` 分发——主窗口是本地直调，子窗口是跨窗口 RPC（4.5 节）。

#### 4.2.4 代码实践

1. **实践目标**：在源码中定位「创建任务、批量/分组创建、读配置」三条调用链的每一跳。
2. **操作步骤**：
   - 从 `ui/flutter/lib/features/tasks/presentation/pages/create_task_window_page.dart` 里 grep `createTaskBatch`，找到提交入口。
   - 向上追 `DownloadSubmission.submit`（`pending_download.dart`）→ `GopeedService.createTaskBatch` / `api.createTaskBatch` → `api.dart:_request` → `NativeGopeedTransport.request`。
   - 对每跳记录：文件、函数、传递的数据类型。
3. **需要观察的现象**：链路上应出现一次「对象 → JSON 字符串 → 对象」的边界（跨 isolate 或跨进程时）。
4. **预期结果**：一张 5~7 行的调用链表。桌面链在 `LibgopeedWorker.invoke` 处进入 isolate 消息；移动链在 `MethodChannel.invokeMethod` 处进入平台通道；两条最终都到达 Go 的同一个 `RouteSpec`（`pkg/api/routes.go` 中 `/api/v1/tasks/batch`，可回看 u8-l1）。
5. 源码阅读型实践，无需运行；如需验证可执行 `cd ui/flutter && flutter analyze`（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`api.dart` 的 `getTasks` 为什么手工拼 `status=xx&status=yy` 查询串，而不是用 `queryParameters` 参数？
**答案**：`_request` 的 `queryParameters` 走 `_RequestParts._encodeQueryParameters` 也能编码列表，但原生传输把查询串作为整体字符串传给 Go Dispatcher（u8-l1 的归一化请求）。两种方式最终都能产生重复键；手工拼接保证 path 与 query 在门面层就定形，与 REST 契约完全一致，传输层无需再理解语义。

**练习 2**：架构文档禁止「特性 Widget 直接 import `api/api.dart`」，那 `api.dart` 还为什么存在？
**答案**：文档 7.1 节说明现有 `lib/api/model/` 与门面是「迁移期遗留」。`api.dart` 目前仍是主窗口本地绑定的实现（`LocalAppCapabilities` 的 registry 绑定到它），并被 Web 登录、forward 等少数路径使用；业务组件应依赖 `GopeedService` 门面，由能力层决定本地直调还是跨窗口转发。

### 4.3 事件订阅：从 Go 回调到 Flutter Stream

#### 4.3.1 概念说明

u8-l4 讲过 Go 侧：`pkg/api/events.go` 用 `TaskEventMask` 位掩码订阅，`SubscribeTaskEvents` 经桥接暴露。Flutter 侧的对应设计很克制——只订阅两个低频终态事件（done/error），不做 EventHub、不做序列号、不做进度合流。进度类更新由任务页自己的轮询完成（Web 一直如此，原生也沿用）。

#### 4.3.2 核心流程

```text
Go Downloader.emit（锁外）
  → TaskEventCallback（pkg/api/events.go，位掩码过滤）
     ├─ 桌面: C 回调 → isolate worker 收 'taskEvent' 消息 → 广播流
     └─ 移动: gomobile listener → MethodChannel 反向 'taskEvent' → 广播流
  → LibgopeedBoot.instance.taskEvents（Stream<TaskEvent>）
  → AppNotificationController 监听 → 系统通知
Web: taskEvents 为空 Stream，页面靠轮询
```

#### 4.3.3 源码精读

订阅点在主窗口启动序列里，紧跟 `start` 之后：

[ui/flutter/lib/app/application/app_runtime_controller.dart:141-142](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/application/app_runtime_controller.dart#L141-L142) `subscribeTaskEvents({TaskEventType.done, TaskEventType.error})`——集合的 `mask` 扩展就是 u8-l4 的位掩码。

消费端是桌面通知控制器（替代了旧的 2 秒轮询）：

[ui/flutter/lib/app/application/app_notification_controller.dart:77](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/application/app_notification_controller.dart#L77) 暴露 `taskEvents` 即 `LibgopeedBoot.instance.taskEvents`；

[ui/flutter/lib/app/application/app_notification_controller.dart:155](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/application/app_notification_controller.dart#L155) 订阅后在回调里发系统通知。

桌面 FFI 侧的事件入口：

[ui/flutter/lib/core/common/libgopeed_ffi.dart:12-19](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/common/libgopeed_ffi.dart#L12-L19) 监听 worker 的原始事件流，解码 JSON 后灌入自己的广播流。

移动侧的反向 MethodChannel（4.1 节已引用）：

[ui/flutter/lib/core/common/libgopeed_channel.dart:16-22](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/common/libgopeed_channel.dart#L16-L22) 原生层把 gomobile listener 收到的 JSON 经 `taskEvent` 反向消息推回 Dart。

Web 空流：

[ui/flutter/lib/core/entry/libgopeed_boot_browser.dart:37](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/entry/libgopeed_boot_browser.dart#L37) `Stream.empty()`——组件无需判平台，订阅空流就是无操作。

#### 4.3.4 代码实践

1. **实践目标**：验证「事件只推 done/error、不推 progress」的策略在代码中的三处体现。
2. **操作步骤**：
   - 找 `subscribeTaskEvents` 的全部调用点（`grep -rn "subscribeTaskEvents" ui/flutter/lib`），确认传入的集合内容。
   - 读 [backend-transport-architecture.md:300-326](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/docs/backend-transport-architecture.md#L300-L326) 第 6 节，记录它拒绝 EventHub 的理由。
   - 对照 `app_notification_controller.dart:155` 附近的回调体，确认回调内没有重活。
3. **需要观察的现象**：调用点应只有启动时一次（外加 stop 时传空集合注销）。
4. **预期结果**：三条证据链与文档 6.1 节「最小任务回调」的描述逐条对上。
5. 源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：如果未来要加「任务开始」事件（`TaskEventStart`），前端需要改几处？
**答案**：按架构文档 8.2 节：Go 侧定义事件名与载荷、在业务状态迁移处 emit、加掩码过滤测试；Flutter 侧加枚举值与载荷解码器。FFI 回调、gomobile listener、MethodChannel 转发都不用动——通用信封已经就位。订阅处把 `TaskEventType.start` 加进集合即可。

**练习 2**：为什么事件的广播流用 `StreamController.broadcast()` 而不是单订阅流？
**答案**：广播流允许多个消费者（通知控制器、未来的其它组件）同时监听同一事件源而互不抢占；单订阅流第一个监听者会独占流，后到者直接抛状态错误。代价是广播流没有缓冲，无监听者时事件被丢弃——对终态通知而言可接受，页面状态由轮询兜底。

### 4.4 统一子表面路由：AppRouter.openWindow、AppWindowPage 与 Web 模态（d69874e）

#### 4.4.1 概念说明

「创建任务」界面需要出现在很多入口：主页按钮、托盘菜单、深链（gopeed:// 协议）、浏览器捕获、分享意图。三种平台上的正确呈现又不同：

- 桌面：独立原生子窗口（可以同时开多个）。
- Web：应用内模态——浏览器里不可能开独立窗口，而且以前 Web 端会跳转离开当前分区、丢失筛选与滚动状态。
- 移动：全页路由。

d69874e 之前，每个入口都自己判断平台并选择呈现，深链控制器里写着「桌面开子窗，失败再回退路由」的分支逻辑。d69874e 把这个决策收敛到一个函数 `AppRouter.openWindow(payload)`：**调用方只声明『打开什么』（payload），不选择『怎么呈现』**。三种形态分别是：

1. 桌面：`AppWindowLauncher.open` 创建独立子窗口；
2. 桌面失败或 Web：`router.push` 一个 `AppWindowPage`（DialogRoute 模态）；
3. 移动：`router.go` 全页。

配套的两个新文件：`app_window_page.dart`（应用内模态的 Page）与 `app_window_content.dart`（目的地注册表，桌面子窗口与模态共用同一份内容构建器）。

#### 4.4.2 核心流程

```text
任意入口（托盘/深链/按钮/浏览器捕获）
  → AppRouter.openCreateTask(createTask)          # 便捷封装
    → AppRouter.openWindow(AppWindowPayload)      # 唯一入口
       ├─ 桌面且 AppWindowLauncher.open 成功 → 返回（独立子窗口）
       │    └─ WindowController.create(arguments: payload.toRaw())
       ├─ 当前已在目标页 → 更新 pendingCreateTask（分享意图竞态）
       ├─ 移动 → router.go('/create')             # 全页
       └─ Web/回退 → router.push('/create')       # AppWindowPage 模态
            └─ AppWindowContent.build(payload, modal: true, onClose: ...)
```

`onClose(created)` 契约：取消（`created=false`）只弹出子表面；完成（`created=true`）选中「下载中」列表并回主页（`showDownloadingTasks`）。

#### 4.4.3 源码精读

统一入口——注意 doc 注释「Callers never need to choose a presentation」：

[ui/flutter/lib/app/router/app_router.dart:36-43](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/router/app_router.dart#L36-L43) `openCreateTask` 是便捷封装，真正逻辑在 `openWindow`：先尝试桌面子窗口，`AppWindowChrome.isDesktopWindow` 保证非桌面不会走这条分支；

[ui/flutter/lib/app/router/app_router.dart:44-62](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/router/app_router.dart#L44-L62) 三条回退：同页去重（把新请求写进 `pendingCreateTaskProvider`，处理移动分享意图在页面已打开时到达）、移动 `router.go` 全页、其余 `router.push` 模态。

路由声明——`/create` 挂在根 Navigator 上，保证模态覆盖整个 shell：

[ui/flutter/lib/app/router/app_router.dart:125-150](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/router/app_router.dart#L125-L150) `pageBuilder` 里按平台二选一：移动 `NoTransitionPage`（全页无动画），否则 `AppWindowPage`（模态）。内容统一由 `AppWindowContent.build` 生成，`onClose` 回调里 `created ? showDownloadingTasks() : pop`。`state.extra` 是序列化的 payload 字符串——文档说明这是为了让浏览器历史能恢复模态及其初始请求。

应用内模态本体——一个自定义 `Page`，产出 `DialogRoute`：

[ui/flutter/lib/app/router/app_window_page.dart:8-24](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/router/app_window_page.dart#L8-L24) 类注释点明关键特性「keeps its underlying page mounted」——模态下面的页面不销毁，筛选与滚动状态得以保留；

[ui/flutter/lib/app/router/app_window_page.dart:25-63](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app_router/app_window_page.dart#L25-L63) 构建细节：`fullScreen: true` 的 DialogRoute + 内层 `ConstrainedBox` 限制到 `preferredSize`（默认 `AppDesignTokens.childWindowSize`），窄屏（<mobile 断点）用小间距、宽屏用大间距，最后用 `ModalContainer` 画出圆角窗口样式。

目的地注册表——桌面子窗口与模态共用：

[ui/flutter/lib/app/router/app_window_content.dart:9-29](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/router/app_window_content.dart#L9-L29) `location()` 把 `AppWindowType` 映射为路由路径（createTask → `/create`），`build()` 按 payload 类型产出 `CreateTaskWindowPage`，`modal` 与 `onClose` 参数只在应用内呈现时传入。要加新子表面，就在这里注册路径与内容。

桌面启动器与回退：

[ui/flutter/lib/core/window/app_window_launcher.dart:13-44](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/app_window_launcher.dart#L13-L44) 每次请求都新建子窗口（创建任务窗口不复用）；`hiddenAtLaunch: true` 让子引擎先完成尺寸/居中/前景设置再显示。失败分支：记录日志、`windowManager.show()+focus()` 把主窗口带回前台（托盘/深链可能在主窗口隐藏时到达），然后返回 false 让 `openWindow` 落到应用内模态。

载荷模型——只携带不可变的启动输入：

[ui/flutter/lib/core/window/app_window_payload.dart:5-55](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/app_window_payload.dart#L5-L55) `type` + `data`（含可选 `createTask`），`toRaw()/fromRaw()` 做 JSON 字符串序列化。文档规定 API 地址、token、外观、持久化数据**禁止**放进 payload——子窗口必须经能力 RPC 获取这些。

入口收敛的证据（d69874e 的 diff）：

[ui/flutter/lib/app/application/app_deep_link_controller.dart:119](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/application/app_deep_link_controller.dart#L119) 深链的 `_openCreate` 由十来行平台分支缩成一行委托 `AppRouter.openCreateTask`；托盘入口在 `app_platform_controller.dart` 中同样收敛。

文档契约：

[ui/flutter/docs/window-capability-rpc.md:147-172](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/docs/window-capability-rpc.md#L147-L172) 第 11 节「Opening Child Surfaces」规定所有入口都必须走 `openWindow`，并列出三种呈现与失败回退、`onClose` 契约、payload 可序列化的理由。

创建任务页对三种宿主的适配：

[ui/flutter/lib/features/tasks/presentation/pages/create_task_window_page.dart:873-913](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/features/tasks/presentation/pages/create_task_window_page.dart#L873-L913) `_closeWindow`/`_finishCreation` 的三分支：有 `onClose`（应用内模态）直接回调；有 `windowController`（独立子窗口）经能力 RPC 导航后关窗；两者皆无（移动全页）`context.go('/')`。

新增的导航测试（351 行）：

[ui/flutter/test/app/router/app_window_navigation_test.dart:34-79](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L34-L79) 用 mock MethodChannel 模拟「子窗口创建不可用」，覆盖 Provider 覆写与整应用挂载，验证模态回退与 onClose 行为。

#### 4.4.4 代码实践

1. **实践目标**：跟踪一次 Web 端「模态创建任务」的完整路径。
2. **操作步骤**：
   - 从 `AppRouter.openCreateTask`（`app_router.dart:36`）出发，沿 `openWindow` → `router.push('/create')` → `/create` 路由的 `pageBuilder` → `AppWindowPage.createRoute` → `AppWindowContent.build` → `CreateTaskWindowPage(onClose:...)` 逐文件记录行号。
   - 特别注意 `extra: payload.toRaw()` 如何在 `pageBuilder` 里被 `AppWindowPayload.fromRaw` 还原，以及浏览器地址栏会出现 `/create` 路径（刷新可恢复）。
   - 最后走一遍 `_finishCreation` 的 `onClose(true)` 分支，确认它调用 `showDownloadingTasks()`（`app_router.dart:64-69`）。
3. **需要观察的现象**（如本地跑 Web 版）：模态弹出时下方页面的滚动位置与筛选保持不变；完成创建后模态关闭、左侧切到下载中列表。
4. **预期结果**：一份包含 6~8 个文件的路径清单，与 4.4.2 的流程图完全对应。若本地有 Flutter 环境，可运行 `cd ui/flutter && flutter test test/app/router/app_window_navigation_test.dart` 佐证（待本地验证）。
5. 纯源码跟踪部分结论确定；运行现象需本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`openWindow` 里为什么要处理「当前已在目标页」的情况（`router.state.uri.path == location`）？
**答案**：移动端分享意图可能在创建页已经打开时再次到达（例如用户在分享面板里连续发两个链接）。如果直接再 push/go，会叠两层页面或丢失已填表单。此分支改为把新请求写入 `pendingCreateTaskProvider`，由已打开的页面接管更新。

**练习 2**：`AppWindowPage` 为什么实现为 `Page` 子类（配合 `pageBuilder`），而不是直接在按钮回调里 `showDialog`？
**答案**：`showDialog` 是命令式的，产生的模态不在路由栈里——浏览器无法后退、无法刷新恢复、深链无法定位。实现为 `Page` 后模态成为路由的一部分：`extra` 里的 payload 字符串让浏览器历史可以恢复模态及其初始请求（文档第 11 节明确要求这一点），也便于导航测试用 `router` 断言。

**练习 3**：桌面子窗口创建失败时，为什么在回退模态前要先 `windowManager.show()` + `focus()`？
**答案**：托盘菜单和深链触发的请求可能发生在主窗口隐藏（最小化到托盘/菜单栏模式）时。若不把主窗口带回前台，模态会 push 到一个看不见的窗口上，用户以为点击无效。见 `app_window_launcher.dart:36-41` 的注释。

### 4.5 窗口能力传输与 macOS 子窗口引擎释放（b0d4522）

#### 4.5.1 概念说明

桌面子窗口是**独立的 Flutter engine**（独立 Dart isolate），它不能加载动态库、不能碰存储、更不能自己再起一个 Go Runtime——「主窗口是唯一 owner」。子窗口的一切业务操作都要跨窗口转发回主窗口执行，这套转发协议就是**窗口能力 RPC**：

- 单一 `WindowMethodChannel`（`gopeed.app.capabilities.v1`），单一方法 `capability.call`，参数为操作名 + JSON 载荷，响应用统一成功/失败信封。
- 每个操作声明为类型化 `RpcMethod<P,R>`，编解码集中在 `RpcCodecRegistry`，本地调用则零序列化直调。
- 主→子的事件（外观同步：主题/主题色/语言）经各子窗口的 `WindowController` 通道推送**完整快照**而非增量补丁。

b0d4522 修的是这套体系的资源泄漏问题，分两层：

1. **Swift 层**：`desktop_multi_window` 会把关闭的窗口移出注册表，但插件通道仍可能持有 window/controller/engine 的引用环，`NSWindow.close` 之后子 Dart isolate 与堆内存继续存活。修复：监听每个子窗口的 `willCloseNotification`，在下一个主队列 tick 里拆引用、`shutDownEngine()`。
2. **Dart 层**：引擎被强杀意味着子窗口的 `Widget.dispose` 不可靠，`window.unsubscribe` 可能永远发不出来，主窗口的 `_subscribers` 表就会积累死条目、外观广播持续向已关闭窗口投递。修复：宿主监听 `onWindowsChanged`，用 `WindowController.getAll()` 的存活集合清洗订阅表（`_removeClosedSubscribers`）。

#### 4.5.2 核心流程

```text
子窗口请求（如 createTaskBatch）
  → WindowCapabilityInvoker.invoke
    → hostChannel.invokeMethod('capability.call', {operation, payload})
  → 主窗口 AppWindowCapabilityHost._handleCall
    → registry.invoke(operation, payload)        # LocalAppCapabilities → api.dart / GopeedService
    → {'ok': true, 'data': ...} 或 {'ok': false, 'error': {...}}

子窗口初始化（ChildWindowSession.connect）
  1. 注册本窗口事件处理器
  2. bootstrap 拉取当前外观快照并应用
  3. subscribe(windowId) → 宿主记录 controller 并立即再推一次快照
  之后: 主窗口外观变化 → broadcastAppearance → 全部订阅者

macOS 子窗口关闭（b0d4522）
  NSWindow.willCloseNotification
    → 移除观察者 → 主队列下一 tick:
        window_manager.onEvent = nil; delegate = nil
        engine.shutDownEngine(); contentViewController = nil
  Dart 宿主: onWindowsChanged → _removeClosedSubscribers
    → WindowController.getAll() 取存活 ID 集
    → 从 _subscribers 删除不在存活集中的条目
```

#### 4.5.3 源码精读

协议常量集中声明——文档第 3 节禁止操作字符串散落各处：

[ui/flutter/lib/core/window/window_capability_transport.dart:11-21](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L11-L21) 通道名、`capability.call`、bootstrap/subscribe/unsubscribe/event、`appearance.changed` 全部在此。

子窗口侧调用器：

[ui/flutter/lib/core/window/window_capability_transport.dart:23-45](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L23-L45) 编码参数 → 发送 → 解码信封；`ok != true` 时抛 `CapabilityException`，业务错误不伪装成传输错误。

宿主的启动与 b0d4522 的关键一行：

[ui/flutter/lib/core/window/window_capability_transport.dart:59-64](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L59-L64) `start` 里订阅 `onWindowsChanged`，每次窗口集合变化都异步调用 `_removeClosedSubscribers()`；

[ui/flutter/lib/core/window/window_capability_transport.dart:74-88](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L74-L88) 清洗实现，注释直说原因：「Engine shutdown does not run Widget.dispose, so a closed child cannot always send window.unsubscribe itself」。先快照候选集，`getAll()` 取存活 ID，`removeWhere` 删掉已消失的订阅者；`identical(registry, _registry)` 防止 stop 后的迟到回调；catch 吞掉关闭期的注册表异常。

宿主的消息分发：

[ui/flutter/lib/core/window/window_capability_transport.dart:96-124](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L96-L124) 四个方法：`capability.call` 查注册表执行、`bootstrap` 返回外观快照、`subscribe` 记录 controller 并补发快照、`unsubscribe` 删除条目；

[ui/flutter/lib/core/window/window_capability_transport.dart:125-142](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L125-L142) 错误信封化：仅 `capability.call` 的异常被转成 `{'ok': false, 'error': ...}`，其余方法直接 rethrow 走 MethodChannel 层错误。

外观广播与投递失败的自我清理：

[ui/flutter/lib/core/window/window_capability_transport.dart:144-158](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L144-L158) 广播前复制订阅表防迭代中修改；单点投递失败即从表中移除该订阅者（第二道防线，与 `_removeClosedSubscribers` 互补）。

子窗口会话：

[ui/flutter/lib/core/window/window_capability_transport.dart:166-203](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L166-L203) `connect` 按文档第 5 节的初始化顺序执行：注册处理器 → bootstrap 拉快照 → subscribe。`dispose` 正常路径会发 `unsubscribe`，但如前所述不能依赖它一定被执行。

子窗口的根 Widget 用外观快照驱动主题：

[ui/flutter/lib/app/child_window_app.dart:82-87](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/child_window_app.dart#L82-L87) 页面内容同样来自 `AppWindowContent.build`（4.4 节的注册表）——这就是「一份内容、三种宿主」的交汇点。

macOS Swift 侧的引擎释放：

[ui/flutter/macos/Runner/MainFlutterWindow.swift:16-18](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/macos/Runner/MainFlutterWindow.swift#L16-L18) 每个子窗口创建回调里挂接 `releaseChildEngineOnClose`；

[ui/flutter/macos/Runner/MainFlutterWindow.swift:27-45](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/macos/Runner/MainFlutterWindow.swift#L27-L45) 注释解释问题（插件通道引用环使 `NSWindow.close` 后子 isolate 与堆仍存活）；实现为 `willCloseNotification` 观察者 + `DispatchQueue.main.async` 延迟拆除：先清 `window_manager` 的 `onEvent` 闭包与 delegate（它们持有 NSWindow），再 `engine.shutDownEngine()`、`contentViewController = nil`。延迟一拍是为了让关闭通知与插件的注册表移除先完成，避免拆掉正在传递关闭请求的引擎。

回归测试（b0d4522 新增）：

[ui/flutter/test/core/window/window_capability_lifecycle_test.dart:21-64](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/core/window/window_capability_lifecycle_test.dart#L21-L64) 场景：两个子窗口 `closed-child`、`live-child` 都订阅后，mock 的 `getAllWindows` 只返回 `live-child`；触发 `onWindowsChanged` 后更新外观，断言事件只投递给 `live-child`——精确复现「无 Dart unsubscribe 也能清理订阅」的行为。

#### 4.5.4 代码实践

1. **实践目标**：跑通 b0d4522 的订阅清理回归测试并解释每个 mock 的作用。
2. **操作步骤**：
   - 读 [window_capability_lifecycle_test.dart:21-64](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/core/window/window_capability_lifecycle_test.dart#L21-L64)。
   - 指出三个 mock 各自模拟什么：`channels` 通道记录宿主向子窗口的投递、`windows` 通道的 `getAllWindows` 返回存活集合、`onWindowsChanged` 平台消息模拟窗口集合变化事件。
   - 有 Flutter 环境时执行 `cd ui/flutter && flutter test test/core/window/window_capability_lifecycle_test.dart`（待本地验证）。
3. **需要观察的现象**：测试断言 `deliveries` 只含 `live-child` 的通道——死订阅者没有收到广播。
4. **预期结果**：测试通过；并能口头回答「如果删掉 `_removeClosedSubscribers`，这个测试会在哪一步失败」（答案：`deliveries` 会包含 `closed-child`，断言不等）。
5. 运行结果待本地验证；源码推理部分结论确定。

#### 4.5.5 小练习与答案

**练习 1**：`_removeClosedSubscribers` 为什么先 `candidates = _subscribers.keys.toSet()` 快照，再 `removeWhere`？
**答案**：`removeWhere` 的谓词执行期间不能并发修改被遍历的表；先做不可变快照，`removeWhere` 的条件写成 `candidates.contains(id) && !openIds.contains(id)`，只处理本次触发时已存在的条目——期间新加入的订阅者不受影响，也不会因迭代器失效崩溃。另外 `identical(registry, _registry)` 检查丢弃 stop 之后到达的迟到回调。

**练习 2**：Swift 侧为什么把引擎拆除放进 `DispatchQueue.main.async`，而不是在关闭通知回调里同步执行？
**答案**：`willCloseNotification` 触发时，关闭流程（包括插件把自己的窗口从注册表移除）尚未结束，此刻同步 `shutDownEngine` 可能拆掉还在传递关闭请求的引擎。延迟到下一个主队列 tick，让关闭通知与注册表清理先完成，再安全拆除。这是典型的「让正在途中的消息落地再收尾」时序处理。

**练习 3**：文档禁止子窗口「import api/api.dart」，那 `CreateTaskWindowPage` 在子窗口里是怎么创建任务的？
**答案**：`CreateTaskWindowPage` 自身不 import api，它注入 `GopeedService`；该服务在主窗口由 `LocalCapabilityInvoker` 本地直调，在子窗口由 `WindowCapabilityInvoker` 经 `capability.call` 跨窗口转发到主窗口的同一注册表执行。页面代码对宿主形态无感知——这正是 4.4 节「一份内容、三种宿主」能成立的前提。

### 4.6 批量与分组创建在前端的呈现：PendingDownload 与 planDownloads

> 本模块是 4.2 的业务落地，对应 u4-l6 的后端分组机制，帮助读者把前后端两条链对上。

#### 4.6.1 概念说明

创建任务页允许用户一次粘贴多个 URL、并预览每个资源的文件树（BT 种子、FTP 目录）。前端用一个领域模型 `PendingDownload` 承担「一个来源」的全部预览状态：resolve 结果、错误、已选文件下标、是否已创建。提交时不是简单地把所有 URL 各建一个任务，而是：

- **分组来源**（一个资源含多个文件且无 resolve 缓存 ID、有根名）：整组作为一个 `CreateTaskBatch(group: true, groupName: ...)`，落到 u4-l6 的组目录。
- **独立来源**（单文件、或有 rid）：合并进一个普通批量请求。

文档第 10 节明确了协议语义：成员 `rid` 非空优先、BT 选择保留原始文件下标、部分成功也算整批完成、失败批量可重试且不重复已成功的批量、不支持向已有组追加成员。

#### 4.6.2 核心流程

```text
用户粘贴 URL → 每个来源一个 PendingDownload
  → resolve(service) 填充 result + 默认全选
  → 用户勾选文件 / 改路径
  → planDownloads(sources):
       分组来源 → 每组一个 DownloadSubmission(group: true)
       独立来源 → 合并为一个 DownloadSubmission
  → 逐个 submission.submit(service):
       service.createTaskBatch(request)
       成功 → 全部 source.created = true
       失败 → 全部 source.createError = error（可整批重试）
```

#### 4.6.3 源码精读

批量模型与后端一一对应：

[ui/flutter/lib/api/model/create_task_batch.dart:8-21](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/api/model/create_task_batch.dart#L8-L21) `CreateTaskBatch`：批级 `opts`、成员列表 `reqs`、`group`、`groupName`；

[ui/flutter/lib/api/model/create_task_batch.dart:23-33](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/api/model/create_task_batch.dart#L23-L33) 成员三选一组合：`rid`（resolve 缓存 ID）或 `req`，加各自的 `opts`；

[ui/flutter/lib/api/model/create_task_batch.dart:35-44](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/api/model/create_task_batch.dart#L35-L44) 响应 `CreateTaskBatchResult`：`taskIds` + 可选 `parentId`（组任务 ID，用于跳转分组卡片）。

单个来源的批量项生成，两条分支：

[ui/flutter/lib/features/tasks/application/pending_download.dart:66-79](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/features/tasks/application/pending_download.dart#L66-L79) 有 `rid` 或无文件树：单个成员（rid 优先），选择下标写入 `opts.selectFiles`，多选时丢弃校验和（多个文件共享一个 checksum 无意义）；

[ui/flutter/lib/features/tasks/application/pending_download.dart:80-107](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/features/tasks/application/pending_download.dart#L80-L107) 文件树逐文件展开：每个文件一个成员，路径拼成 `父路径/资源名/文件路径`；`asDefaultPath = false`（父目录由用户单独持久化，成员不抢默认目录）、`skipAutoCategorize` 按规则置位（对应 u10-l1 的分类旁路）；FTP 来源把连接数夹到 32 以内；非 HTTP 协议 `size` 置 0（BT 等大小由后端定）。

提交与失败重试语义：

[ui/flutter/lib/features/tasks/application/pending_download.dart:114-136](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/features/tasks/application/pending_download.dart#L114-L136) `DownloadSubmission` 持整批来源与请求；成功标记全部 `created`，失败给全部来源写 `createError`——「一个成功的批量完成整个提交」，重试时以 Submission 为单位，不会重复已成功的批量。

分组规划：

[ui/flutter/lib/features/tasks/application/pending_download.dart:138-166](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/features/tasks/application/pending_download.dart#L138-L166) `planDownloads` 把来源分流：`isGroup` 的各自成组（组名取用户重命名或资源根名），其余合并为一个普通批量。分组请求在前、独立请求在后。

文档契约：

[ui/flutter/docs/window-capability-rpc.md:134-145](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/docs/window-capability-rpc.md#L134-L145) 第 10 节「Batch Task Creation」：rid 优先、保留原始文件下标、部分成功即完成、失败批量可重试、不支持追加已有组。

#### 4.6.4 代码实践

1. **实践目标**：用源码推演两个场景的请求体差异。
2. **操作步骤**：
   - 场景 A：一个 BT 种子（resolve 得到 5 个文件，勾选 2 个）。写出 `creationItems()` 返回的成员：应有一个成员、`rid` 非空、`opts.selectFiles` 为排序后的两个下标。
   - 场景 B：一个 FTP 目录（resolve 无 ID、根名非空、3 个文件全选）。写出 `planDownloads` 的输出：一个 `group: true` 的 Submission，含 3 个成员，各自 `path` 为 `用户目录/根名/文件路径`。
   - 对照 `creationItems()` 的两个分支逐行验证你的推演。
3. **需要观察的现象**：纸面 JSON 与 4.6.3 引用的分支代码逐字段吻合。
4. **预期结果**：两份请求体 JSON 草图。如需真实验证：运行 Web 版粘贴一个公开 BT 磁力与一个 FTP 目录，用浏览器开发者工具看 POST `/api/v1/tasks/batch` 的请求体（待本地验证）。
5. 推演部分结论确定；网络抓包需本地验证。

#### 4.6.5 小练习与答案

**练习 1**：为什么多文件选择时 `_omitChecksum` 要把 checksum 从 extra 里删掉？
**答案**：HTTP 的 checksum 是对单一最终文件的哈希校验（u5-l4）。批量提交会产生多个文件，一个 checksum 无法同时描述多个产物；保留它会让后端对一个随机成员做校验从而误报。删除是最安全的语义：批量场景放弃校验。

**练习 2**：`PendingDownload.isGroup` 的判定为什么要求 `result.id.isEmpty`（无 resolve 缓存 ID）？
**答案**：`rid` 是「已 resolve 且结果被后端缓存」的标志，走 rid 的成员由后端从缓存拿完整资源，无需前端展开文件树。而分组路径是前端把多文件逐个展开成独立请求再建组，两者互斥：有 rid 说明单个资源整体成一个任务（文件选择用 `selectFiles` 表达），没有 rid 且多文件才需要前端拆成员、走 `group: true`。

## 5. 综合实践

**任务：产出一份「Gopeed 前端对接速查手册」**，把本讲五条线索串成一张可长期维护的文档：

1. **三端通信策略表**：精读 [backend-transport-architecture.md](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/docs/backend-transport-architecture.md) 第 2、5、6 节与 [window-capability-rpc.md](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/docs/window-capability-rpc.md)，做一张「桌面 / 移动 / Web ×（启动、业务调用、事件、子表面、文件访问）」五行策略表，每格注明关键源码文件。
2. **四条调用链定位**（在 Flutter 源码中逐跳记录文件与行号）：
   - 创建任务：入口按钮 → `openCreateTask` → … → `api.createTask`；
   - 批量/分组创建：`create_task_window_page` → `planDownloads` → `DownloadSubmission.submit` → `api.createTaskBatch`；
   - 订阅事件：`app_runtime_controller` 启动 → `subscribeTaskEvents` → `app_notification_controller` 消费；
   - 读取配置：设置页 → `gopeedServiceProvider.getConfig` → `api.getConfig`。
3. **两条跟踪路径**：
   - Web 模态创建任务：`AppRouter.openCreateTask` → `AppWindowPage`（DialogRoute）→ `AppWindowContent.build` → `onClose(true)` → `showDownloadingTasks`；
   - macOS 子窗口关闭清理：`MainFlutterWindow.releaseChildEngineOnClose`（Swift 拆引擎）与 `AppWindowCapabilityHost._removeClosedSubscribers`（Dart 清订阅），并说明两层防线为何缺一不可。
4. **验证**（可选，待本地验证）：`cd ui/flutter && flutter test test/app/router/app_window_navigation_test.dart test/core/window/window_capability_lifecycle_test.dart`，把两条测试的断言与手册里的路径一一对应。

预期产出：一份 Markdown 手册，任何人拿它能在一分钟内回答「某功能在某平台上走哪条链、经过哪些文件」。

## 6. 本讲小结

- Flutter 侧的通信选型是「**一套业务方法、三种传输**」：`api.dart` 门面 + `GopeedTransport`（Web REST / 桌面 FFI / 移动 gomobile MethodChannel），传输层只搬运不实现业务，新增 API 三端零重复。
- 启动引导 `libgopeed_boot` 用条件导入分三端：原生 `start` 强制 bolt 存储并关掉 Go 侧高频刷新；Web 是空壳（REST 客户端），事件流为空、页面靠轮询。
- 事件管道刻意极简：只订阅 done/error 两个终态事件（位掩码），桌面经 isolate worker、移动经反向 MethodChannel 汇入同一个广播流；进度类更新不推送。
- d69874e 把「打开子表面」收敛为唯一入口 `AppRouter.openWindow`：桌面独立子窗口、Web 应用内模态（`AppWindowPage` DialogRoute，底层页面保持挂载）、移动全页，失败自动回退模态；`AppWindowContent` 让三种宿主共享同一份内容构建器。
- 窗口能力 RPC 规定子窗口永不持有 Go Runtime：一切经 `capability.call` 转发回主窗口；主→子事件推送完整外观快照。
- b0d4522 双层修复子窗口泄漏：Swift 层在 `willClose` 后延迟 `shutDownEngine` 拆引用环，Dart 层 `_removeClosedSubscribers` 用存活窗口集合清洗订阅表（因为引擎强杀使 `dispose`/`unsubscribe` 不可靠）。
- 前端的批量/分组创建由 `PendingDownload` + `planDownloads` + `DownloadSubmission` 承担：分组来源各自成组、独立来源合并一批，rid 优先、部分成功即完成、失败以批为单位重试，与 u4-l6 的后端语义严格对齐。

## 7. 下一步学习建议

- **u11-l4 毕业实践**：本讲的三条调用链（配置 → 模型 → Downloader → API → 前端）正好是毕业特性要贯穿的路径，建议直接进入。
- **想深入前端测试**：读 `ui/flutter/test/app/router/app_window_navigation_test.dart`（351 行）如何用 mock MethodChannel 与 Provider 覆写测整应用导航，对照 u11-l1 的 Flutter 测试组织方式。
- **想深入多窗口与平台集成**：读 `ui/flutter/lib/core/window/app_window_bootstrap.dart`（子窗口尺寸/居中/chrome 设置，解释 `hiddenAtLaunch` 的后半段故事）与 `MainFlutterWindow.swift` 全文，理解 macOS 菜单栏模式与窗口生命周期的交互。
- **想回顾后端对应面**：u8-l3（InvokeAsync 执行器与停止排水）、u8-l4（位掩码事件订阅）、u4-l6（TaskGroup 与批量 API）分别对应本讲的 4.1/4.2、4.3、4.6，建议交叉重读一遍把前后端链路闭上。
