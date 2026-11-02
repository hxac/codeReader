# 测试体系与 CI

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 Gopeed 各层测试（协议级、下载器级、服务层、REST 层、webview 集成、Flutter 窗口级）分别放在哪里、怎么组织、用什么测试替身。
2. 理解 `internal/test` 的 HTTP 假服务器家族——每台「坏服务器」模拟哪一种线上故障。
3. 读懂上一轮提交（1e36599）新增的约 1100 行批量/分组任务测试：它们如何用内存协议、存储装饰器和通道同步复现竞态与故障。
4. 读懂本轮提交（cd19772）新增的三个 BT 共享客户端竞态回归测试：它们复现哪类「构建与关闭」竞态、修复后的锁窗口如何消除它。
5. 了解 d69874e/b0d4522 两个 Flutter 提交配套的窗口导航测试与能力生命周期测试的组织方式。
6. 会用 `cmd/extmock` 在不启动 Flutter UI 的情况下调试扩展。
7. 能在本地复现 CI 的测试命令，理解 `-tags webview`、`xvfb-run`、`-coverpkg` 与多 OS 矩阵各自解决什么问题。

## 2. 前置知识

- **测试替身（test double）**：真实网络、真实磁盘、真实浏览器在测试里既慢又不可复现。替身是用受控实现替换真实依赖的艺术：假 HTTP 服务器（替网络）、内存 Fetcher（替协议）、存储装饰器（替磁盘故障）、mock MethodChannel 与能力注册表（替 Flutter 的原生窗口与 Go 后端）、无头 WebView（替浏览器）。
- **表驱动测试**：Go 惯用法，把「输入 + 期望输出」列成结构体切片循环执行，一个函数覆盖十几种边界。本讲的分组目录白名单测试就是典型。
- **白盒测试**：`pkg/download/downloader_test.go`、`internal/protocol/bt/fetcher_test.go` 与被测代码在**同一个包**里，测试可以直接访问私有字段与包级变量（如 `d.groups`、`d.waitTasks`、BT 的 `lock`/`client`/`closeClient`），这是它能精确断言内部状态的原因。
- **通道同步（channel synchronization）**：要测试「删除进行到一半时另一个删除并发到达」这类竞态，测试用 `entered`/`release` 一对通道把被测代码精确卡在临界区中间，比 `time.Sleep` 可靠得多。BT 竞态测试则换一种手法：**用循环重复制造线程交叠窗口**（100 次初始化即关闭、两协程各 10 轮解析+关闭），配合 `go test -race` 的竞争检测器暴露数据竞争。
- **竞态回归测试（race regression test）**：修复并发 bug 后，写一个「反复制造交叠窗口」的测试钉住它。它不追求确定性复现某次交错，而是靠高频率重复让坏时序大概率出现；再以 `-race` 在检测到数据竞争时直接失败。
- **build tags 与 xvfb**：`-tags webview` 会编译依赖 WebKit/GTK 的真实 WebView 代码（需要 CGO）；`xvfb-run` 在无显示器的 CI 机器上提供虚拟 X 显示。二者配合才能在 Linux 云端跑浏览器级测试。
- 前置讲义：u3-l4（自定义协议 Fetcher，本讲综合实践为它补测试）、u4-l2（任务主链路）、u4-l6（TaskGroup 与批量创建，分组测试的主角）、u6-l1（BT 共享客户端设计，竞态测试的主角）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `internal/test/httptest.go` | HTTP 假服务器家族：慢速、限连、忽略 Range、一次性 URL、过期重定向、Cookie 过期等十余种「坏服务器」 |
| `internal/test/util.go` | 校验工具：`FileMd5`/`DirMd5`（完整性断言）、`ToJson`/`JsonEqual` |
| `pkg/download/downloader_test.go` | 下载器级测试主战场（5000+ 行）：`generationTestManager` 测试协议、blob、调度竞态、批量/分组任务 |
| `pkg/download/category_test.go` | 自动分类与目录决策的表驱动单测（含分组白名单测试） |
| `pkg/download/extension_test.go` | 扩展测试 + 共享辅助：`setupDownloader`、`waitForTaskStatus` |
| `internal/protocol/bt/fetcher_test.go` | BT 协议测试：种子/磁力解析、Patch、peer 发现，以及 cd19772 新增的三个共享客户端竞态回归测试 |
| `pkg/api/service_test.go` | 服务层测试：不启动 HTTP 服务器，直接 `Service.JSON` 进程内调用 |
| `pkg/rest/server_test.go` | REST 层测试：真实 HTTP 往返 + Mem/Bolt 双存储矩阵 |
| `cmd/extmock/main.go` | 扩展模拟调试 CLI：resolve/download/eval 三个子命令 |
| `ui/flutter/test/app/router/app_window_navigation_test.dart` | Flutter 窗口导航测试（d69874e 新增，351 行）：模态创建任务、历史恢复、移动全页、重复 share |
| `ui/flutter/test/core/window/window_capability_lifecycle_test.dart` | 窗口能力生命周期测试（b0d4522 新增）：关闭的子窗口引擎失去订阅 |
| `.github/workflows/test.yml` | CI：单元测试 + 覆盖率 + Flutter 侧测试 + 三 OS webview 集成 + 四类构建检查 |
| `internal/webview/goprovider/provider_integration_test.go` | webview 集成测试入口（`-tags webview` 门禁） |

## 4. 核心概念与源码讲解

### 4.1 internal/test 辅助：可控的 HTTP 假服务器

#### 4.1.1 概念说明

HTTP 下载器的绝大多数 bug 都发生在「服务器行为不理想」时：Range 被忽略、重定向过期、Cookie 中途失效、连接数被限……这些场景没法靠一个正常服务器测出来。`internal/test/httptest.go` 的思路是：**每一种线上故障，造一台专门的假服务器**。它放在 `internal/` 下，意味着只有本项目测试能引用，不对外承诺 API 稳定。

所有服务器共享一份测试资产：常量 `BuildName`/`BuildSize`（200MB 的 `build.data`），见 [internal/test/httptest.go:L24-L43](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L24-L43)。注意 `BuildSize = 200 * 1024 * 1024`——每启动一台服务器都会生成一个 200MB 随机文件，这是完整测试套件较重的原因之一。

#### 4.1.2 核心流程

一台假服务器的生命周期：

```text
StartTestXxxServer()
  ├─ net.Listen("tcp", "127.0.0.1:0")     ← 随机端口，杜绝端口冲突
  ├─ 生成 200MB 随机 BuildFile             ← 数据随机，防「碰巧对齐」
  ├─ go server.Serve(listener)            ← 后台服务
  └─ 返回 shutdownListener
测试体：用 "http://<addr>/build.data" 下载、断言
listener.Close()
  ├─ server.Shutdown() 等待在途请求
  ├─ isShutdown = true（让慢速拷贝循环退出）
  └─ 删除 build.data / download.data / 重命名产物
```

#### 4.1.3 源码精读

**公共底座 `startTestServer`**：随机端口 + 随机数据 + 自清理，见 [internal/test/httptest.go:L836-L871](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L836-L871)。`shutdownListener.Close()` 先优雅关停再删三个产物文件，保证测试之间互不污染（[internal/test/httptest.go:L879-L893](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L879-L893)）。

**Range 请求处理核心 `rangeFileHandle`**：手工解析 `Range: bytes=start-end`、Seek 到起点、设置 `Content-Range`/`Content-Length` 并返回 206，见 [internal/test/httptest.go:L705-L779](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L705-L779)。它的 `modifyEnd` 钩子允许服务器「故意多给数据」——`StartTestRangeBugServer` 用它制造 `Range: bytes=0-100` 却返回 150 字节的违规响应（[internal/test/httptest.go:L679-L703](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L679-L703)），正好对应 u5-l3 讲过的 Range 完整性校验。

**坏服务器家族速查表**（都在 [internal/test/httptest.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go) 中）：

| 服务器 | 模拟的故障 | 位置 |
| --- | --- | --- |
| `StartTestOneTimeServer` | 一次性签名 URL：首个请求成功、后续 404（`atomic.Bool` 保证并发下也只放行一次） | [L313-L336](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L313-L336) |
| `StartTestNoRangeSlowServer` | 不支持 Range 的慢速服务器 | [L340-L373](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L340-L373) |
| `StartTestIgnoredRangeServer` | 声称支持 Range（`Accept-Ranges: bytes`）却恒返 200 全量 | [L377-L412](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L377-L412) |
| `StartTestExpiringRedirectServer` | 重定向 URL 会过期（403），逼客户端回原 URL 重新拿新重定向 | [L421-L473](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L421-L473) |
| `StartTestConLimitServer` | 并发连接数超限返回 403（慢启动控制器的收缩测试） | [L526-L551](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L526-L551) |
| `StartTestFailThenRecoverServer` | 先连续失败后恢复；用 **416** 而非 500，因为 5xx 豁免于失败计数（u5-l3 讲过该规则） | [L634-L675](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L634-L675) |
| `StartTestPatchURLServer` | `/bad-url` 恒 404 + `/good-url` 正常，测「换 URL 续传」 | [L928-L949](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L928-L949) |
| `StartTestCookieExpiringServer` | Cookie 中途过期（401），测「换 Cookie 续传」 | [L956-L1004](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L956-L1004) |
| `StartSocks5Server` | 本地 SOCKS5 代理（可带账号密码），测代理解析 | [L902-L921](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L902-L921) |
| `StartTestCustomServer` | 文件名解析大礼包：`Content-Disposition`、RFC 5987 `filename*`、GBK 编码、HTML 实体 `&amp;`、加号编码、403 等 | [L70-L233](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L70-L233) |

**`internal/test/util.go`** 提供「下载是否真的对」的最终裁决：`FileMd5` 对比源文件与产物（[internal/test/util.go:L12-L28](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/util.go#L12-L28)），`AssertResourceEqual` 在忽略 ctime 后深比较 Resource（[internal/test/httptest.go:L1006-L1012](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/test/httptest.go#L1006-L1012)）。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到假服务器的「启动—服务—自清理」闭环。
2. **操作步骤**：运行只依赖文件服务器的单个测试，并用 `-v` 观察输出：
   ```bash
   go test -v -run 'TestDownloader_CreateDirectBatch$' ./pkg/download/
   ```
   然后运行 `TestDownloader_Resolve`（[pkg/download/downloader_test.go:L241-L272](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L241-L272)），它在 `StartTestCustomServer` 上请求 `disposition`、`gbk-encoded` 等端点。
3. **需要观察的现象**：测试期间工作目录出现 `build.data`（约 200MB）与下载产物，测试结束后二者均被 `Close()` 删除；`-v` 下每个子测试名（表驱动的 `name` 字段）逐条打印。
4. **预期结果**：全部 PASS，目录干净。若中途 Ctrl+C 强杀，残留的 `build.data` 需要手动删除——这正是 `Close()` 承担清理职责的原因。
5. 以上命令为标准 `go test` 用法，可本地直接验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StartTestFailThenRecoverServer` 特意用 416 而不是 500 来制造「可计数的失败」？
**答案**：u5-l3 讲过 HTTP Fetcher 的重试规则——5xx 与 429 豁免于失败计数（会无限重试），而 416 会计入失败上限。要测试「失败 N 次后 `Start()` 重试成功」，必须用会被计数的错误码，否则重试永远不会停止、测试永不返回。

**练习 2**：`rangeFileHandle` 的 `modifyEnd` 参数解决了什么测试难题？
**答案**：让服务器「合法地违反协议」——响应头声称 `Content-Range: 0-100/…` 却实际多写 50 字节。不修改 `Content-Length` 只多给数据，正好构造出「响应体比声明长」的违规样本，用来验证客户端会拒绝而非默默截断。

**练习 3**：`AssertResourceEqual` 为什么要先把 `got.Files[0].Ctime` 置 nil 再比较？
**答案**：`Ctime` 是 Resolve 时刻的当前时间，每次运行都不同；不剥离的话 `reflect.DeepEqual` 必然失败。这是「剥离不稳定字段后再深比较」的常见手法。

### 4.2 批量与分组任务测试：测试替身与新测试精读

#### 4.2.1 概念说明

假 HTTP 服务器替的是**网络**，但很多下载器行为（排队、分组、删除恢复、竞态）根本不需要走网络——它们需要一个**假协议**。`generationTestManager` 就是注册进 Downloader 的最小 FetcherManager（u3-l4 的 `mock://` 协议正是参考它实现的）。上一轮提交（1e36599）在 `pkg/download/downloader_test.go` 新增了 28 个测试函数、约 1100 行，全部建立在这套替身之上，覆盖 u4-l6 讲过的 TaskGroup 分组与 `CreateDirectBatchWithResult` 批量语义。

#### 4.2.2 核心流程

分组批量测试的三层替身：

```text
第一层：generationTestManager（假协议）
  ├─ holdOpen=true   → Start() 后任务永远停在 running，便于断言队列
  ├─ resolveStarted/resolveRelease、pauseStarted/pauseRelease
  │     → 用通道把 Resolve/Pause 精确卡住，制造竞态窗口
  └─ starts/pauses 原子计数器 → 断言 Start/Pause 被调了几次

第二层：taskGroupTestStorage（存储装饰器）
  ├─ 嵌入真实 Storage，仅拦截 Put/Delete
  ├─ beforePut(task) 钩子 → 记录首次落盘 / 注入子任务写失败
  └─ failGroupPut / failGroupDelete → 让 task_group 桶读写失败

第三层：groupFailureConcurrentDeleteStorage（并发卡点装饰器）
  └─ Delete 命中 blockedID 时 close(entered) 并阻塞等 release
        → 测试主协程据此在「删除进行到一半」时发起第二个操作
```

#### 4.2.3 源码精读

**① `generationTestManager`/`generationTestFetcher`：内存协议**。管理器只按 `generation://` 前缀匹配（[pkg/download/downloader_test.go:L110-L126](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L110-L126)）；Fetcher 的 `Resolve` 返回 1 字节单文件资源、`Start` 在 `holdOpen=false` 时立刻向 `done` 通道发 nil（[pkg/download/downloader_test.go:L128-L161](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L128-L161)）。配套的 `recoveryTestManager` 补上 `Store`/`Restore`，专门服务断点恢复类测试（[pkg/download/downloader_test.go:L191-L217](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L191-L217)）。

**② 分组测试脚手架**。`newTaskGroupDownloader` 一步造出「单协议 + 内存存储 + holdOpen」的 Downloader，并直接改写私有字段 `d.cfg.MaxRunning`（白盒优势），见 [pkg/download/downloader_test.go:L4048-L4060](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4048-L4060)；`taskGroupBatch` 生成 `Group:true, GroupName:"folder"` 加 N 个 `generation://file/i` 成员的批量请求（[pkg/download/downloader_test.go:L4062-L4068](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4062-L4068)）。

**③ 存储装饰器**。`taskGroupTestStorage` 嵌入 `Storage` 接口，按桶名拦截：`failGroupPut` 让 `task_group` 桶写入失败，`beforePut` 让普通任务落盘前注入错误（[pkg/download/downloader_test.go:L4161-L4184](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4161-L4184)）。有了它，`TestDownloader_TaskGroupPartialFailureAndFirstSave` 才能让 3 个成员中间那个写盘失败、验证「部分成功」——批量为 3 只建成 2 个，且**首次落盘就带 ParentID**（[pkg/download/downloader_test.go:L4186-L4224](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4186-L4224)）。

**④ 分组元数据与重启恢复**。`TestDownloader_TaskGroupMetadataAndRestore` 用真 `BoltStorage` 验证三件事：组不是任务（`GetTask(parentID) == nil`）；成员任务的 JSON 序列化含 `"group":` 但不含 `"type":`（组不会混进任务列表）；`Close()` 后重开同目录，`task.group` 指针被 Setup 重新挂接、删除组 ID 连带清空 `d.groups`（[pkg/download/downloader_test.go:L4070-L4110](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4070-L4110)）。这正对应 u4-l6 讲的「ParentID 落盘为权威、group 指针为运行时缓存」双轨制。

**⑤ 队列与按组操作**。`TestDownloader_TaskGroupQueue` 以 `MaxRunning=5` 提交 6 成员批（叠加 0/2 个既有任务），断言 `running=5-existing`、`waiting=1+existing`；再按「组 ID + 成员 ID」暂停，验证全部成员进入 pause 且 `d.waitTasks` 清空、`expandGroupTaskIDs` 去重后仍为 6（[pkg/download/downloader_test.go:L4112-L4159](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4112-L4159)）。

**⑥ 删除失败的持久化恢复**（u4-l4 讲义核心机制的测试证据）：

- `TestDownloader_TaskGroupDeletePersistence`：删除成员失败后，任务**必须仍在内存**且**必须仍在 `bucketTask` 桶里**——测试直接 `storage.Get(bucketTask, …)` 验证落盘恢复（[pkg/download/downloader_test.go:L4367-L4385](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4367-L4385)）。
- `TestDownloader_TaskGroupConcurrentDelete`：用 `entered`/`release` 通道把成员 A 的删除卡在存储层，此刻删掉成员 B——断言组元数据**不被并发清理误删**（[pkg/download/downloader_test.go:L4387-L4426](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4387-L4426)）。
- `TestDownloader_TaskGroupDeleteFailureRestoresQueue`：删除一个 wait 态成员失败后，它必须回到 `d.waitTasks` 队列；随后删掉 running 成员，被保留者应被唤醒进入 running（用共享辅助 `waitForTaskStatus` 轮询断言，[pkg/download/downloader_test.go:L4479-L4501](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4479-L4501)；该辅助定义于 [pkg/download/extension_test.go:L2042-L2063](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/extension_test.go#L2042-L2063)，done 态还要等 `watchedTasks` 登记清除，避免竞态误判）。

**⑦ 批量三规则的直接验证**。u4-l6 讲过「rid 优先、成员 opts 整体替换、部分成功」，`TestDownloader_BatchMixedResolvedAndDirect` 把它们压进一个批次：`Rid:"missing"`（跳过）、`Rid+Req 并存`（rid 赢，URL 是 `unsupported://` 仍按已解析资源建任务）+ `SelectFiles:[1]`（选择保留）、纯 `Req` 直建、空成员（跳过）；再断言成功的 rid 已从 `fetcherCache` 消费、失败的 rid 不阻断后续合法成员（[pkg/download/downloader_test.go:L4801-L4842](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4801-L4842)）。全败路径由 `TestDownloader_BatchAllFailedAndResolvedSelection` 覆盖：报错、rid **不被**消费、空组与预留目录全部回收（[pkg/download/downloader_test.go:L4844-L4869](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4844-L4869)）；「启动失败但已落盘的任务算成功且保留成员资格」由 `TestDownloader_BatchStartFailureKeepsCreatedTasks` 覆盖（[pkg/download/downloader_test.go:L4871-L4894](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L4871-L4894)）。

**⑧ 上层测试如何接力**。三层测试各自换一种「驱动方式」验证同一批语义：

- **服务层**（不启 HTTP 服务器）：`TestBatchResolvedAndDirectCreation` 用泛型辅助 `serviceTestRequest` 直接调 `Service.JSON`，循环 `grouped=false/true` 断言响应字段——非分组只有 `taskIds`、分组多出 `parentId`；全败批次返回错误信封（[pkg/api/service_test.go:L271-L331](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/api/service_test.go#L271-L331)，辅助在 [L71-L90](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/api/service_test.go#L71-L90)）。
- **REST 层**（真实 HTTP 往返）：`TestCreateDirectTaskBatchWithGroup` 断言响应类型已从 ID 数组改为 `CreateTaskBatchResult`，且任务列表只含真实任务、每个成员带 `parentId` 与 `group` 元数据（[pkg/rest/server_test.go:L250-L282](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/rest/server_test.go#L250-L282)）。注意 `doTest0` 会让**同一个测试跑两遍**——`StorageMem` 与 `StorageBolt`（[pkg/rest/server_test.go:L1207-L1260](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/rest/server_test.go#L1207-L1260)），存储矩阵内建在测试里。
- **分类目录**（纯单测、无 Downloader Setup）：新增的 `TestDecideGroupDownloadDirWhiteList` 是表驱动范本——组根/嵌套成员放行、组外成员与权限被收回的嵌套路径拒绝、字面量 `%year%` 组名不展开；`newUnitCategoryDownloader` 只挂配置不跑 Setup（[pkg/download/category_test.go:L237-L268](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/category_test.go#L237-L268)，脚手架在 [L19-L51](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/category_test.go#L19-L51)）。

#### 4.2.4 代码实践

1. **实践目标**：跑通并读懂分组批量与删除恢复测试。
2. **操作步骤**：
   ```bash
   # 全部 TaskGroup 相关测试（约 20 个，含子测试）
   go test -v -run 'TestDownloader_TaskGroup' ./pkg/download/
   # 批量语义三规则
   go test -v -run 'TestDownloader_Batch' ./pkg/download/
   # 上层接力：服务层与 REST 层
   go test -v -run 'TestBatchResolvedAndDirectCreation' ./pkg/api/
   go test -v -run 'TestCreateDirectTaskBatchWithGroup' ./pkg/rest/
   ```
   然后精读 `TestDownloader_TaskGroupDeleteFailureRestoresQueue`，对照 u4-l4 讲的「deletingTasks 标记、失败回写 bucketTask 与队列恢复」画出测试断言与被测代码的对应关系。
3. **需要观察的现象**：`-run 'TestDownloader_TaskGroup'` 会输出约 20 个 PASS 的顶层测试名；`TestDownloader_TaskGroupQueue` 下有 `0`/`2` 两个子测试名；REST 层测试每个用例打印两遍（Mem/Bolt 两轮）。
4. **预期结果**：全部 PASS。这些测试不访问外网、只依赖 `t.TempDir()`，在任何环境结果应一致。
5. 以上为标准 `go test` 命令，可本地直接验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `newTaskGroupDownloader` 要把 `holdOpen` 设为 true？如果设为 false，`TestDownloader_TaskGroupQueue` 的哪个断言会失效？
**答案**：`holdOpen=true` 让 `Start()` 不向 `done` 通道发结果，任务停留在 running。若为 false，先抢到运行位的成员会瞬间完成、腾出空位，`running=5-existing / waiting=1+existing` 的精确计数就会漂移——测试会变成「看谁跑得快」的竞态赌局。

**练习 2**：`taskGroupTestStorage` 与 `groupFailureConcurrentDeleteStorage` 都是存储装饰器，为何还需要第二种？
**答案**：前者只能让 Put/Delete **确定性地失败**；后者要在「删除执行到一半」的瞬间让另一个操作插进来，这需要 `close(entered)` 通知主协程「我已进入临界区」、`<-release` 等待放行——用通道把时序变成测试可精确控制的确定性事件，而不是靠 sleep 撞运气。

**练习 3**：`TestDownloader_BatchMixedResolvedAndDirect` 里 `Rid` 与 `Req` 同时存在且 `Req.URL` 是 `unsupported://`，为什么任务还能建成？
**答案**：批量规则「rid 优先」——有有效 rid 时直接消费 `fetcherCache` 里已解析好的 Fetcher，`Req` 仅作为兜底字段被忽略，因此其 URL 是否可路由无关紧要。这正是 u2-l1/u4-l6 讲过的 rid 优先语义的测试证据。

### 4.3 BT 共享客户端竞态回归测试（cd19772 新增）

#### 4.3.1 概念说明

u6-l1 讲过 BT 协议的核心设计：**全进程共享一个 `torrent.Client`**，懒加载创建；单任务 `Close` 只 Drop 自己的种子，客户端本体由 `FetcherManager.Close` 在 Downloader 关闭序列中统一销毁。共享意味着两类操作会并发出现在同一批全局变量（`client`、`cfg`、`closeCtx`/`closeFunc`，均受包级 `lock` 保护）上：

- **构建方**：某任务正在 `initClient()` 建客户端、接着 `AddTorrentSpec` 添加种子；
- **关闭方**：另一任务（或 Downloader 关闭）正在 `closeClient()` 销毁客户端并重置 `closeCtx`。

提交 cd19772（PR #1587）修复了三方竞态：旧代码里 `initClient` 自己加锁但锁只覆盖初始化，`AddTorrentSpec` 在锁外执行——`closeClient` 可以插在「初始化完成」与「种子添加完成」之间，把客户端从脚下抽走；此外 `go func() { dnsResolver.Run(closeCtx) }()` 闭包捕获变量，协程真正运行时 `closeClient` 可能已把 `closeCtx` 重置成新值，导致协程读到错误的 context。修复手法有三：

1. `initClient` 改为「**必须持锁调用**」的契约（不再自己加锁）；
2. 新增 `addTorrentSpec`，把锁的覆盖范围**从客户端初始化延伸到种子添加完成**；
3. `closeCtx` 以**参数**形式传给 `dnsResolver.Run` 协程，而非闭包捕获。

#### 4.3.2 核心流程

修复前后的临界区对比：

```text
修复前（竞态窗口敞开）：
  协程 A（Resolve）                     协程 B（Close）
  ─────────────────────────            ─────────────────────
  initClient:
    lock.Lock()
    client = NewClient(cfg)             ← 若 B 在 A 的 lock 释放后进入
    lock.Unlock()                       closeClient: 销毁 client、重置 closeCtx
  client.AddTorrentSpec(spec)           ← 无锁！此刻 client 可能已被销毁
                                       ←（或正被销毁）→ 数据竞争/崩溃

修复后（单一临界区罩住两步）：
  addTorrentSpec:
    lock.Lock()                         closeClient:
    defer lock.Unlock()                   lock.Lock() ← 必须等 A 完成「初始化+添加」
    initClient()    （约定持锁调用）
    client.AddTorrentSpec(spec)
    lock.Unlock()
```

配套细节：`closeCtx` 改为 `go dnsResolver.Run(closeCtx)` 参数传递，协程拿到的是创建那一刻的值，之后 `closeClient` 重置全局变量也不影响它。

#### 4.3.3 源码精读

**① 「必须持锁调用」契约**。函数上方的注释就是契约文本，`initClient` 内部不再出现 `lock.Lock()`，见 [internal/protocol/bt/fetcher.go:L70-L95](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L70-L95)（L70 注释声明契约；函数体末尾 L91-L93 创建 cancel context 并**以参数**启动 DNS 解析协程，L92 的注释解释了原因：closeClient 可能在协程运行前重置 closeCtx）。

**② `addTorrentSpec` 的持锁窗口**。注释直接写明设计意图「holds lock from client initialization until the torrent is added, so closeClient cannot close or reset the client in between」，见 [internal/protocol/bt/fetcher.go:L97-L107](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L97-L107)：加锁 → 持锁调 `initClient` → `client.AddTorrentSpec(spec)` → 解锁。原先 `addTorrent` 里对 `initClient` 的调用被移除，`AddTorrentSpec` 的调用点改为走这个新函数。

**③ 三个竞态回归测试**（都在 [internal/protocol/bt/fetcher_test.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go)）：

| 测试 | 复现的竞态场景 | 关键手法 |
| --- | --- | --- |
| `TestFetcher_CloseAfterInit`（[L75-L92](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L75-L92)） | 「刚初始化完就关闭」：init 与 close 高频背靠背 | 白盒：手动 `lock.Lock()` → `initClient()` → `lock.Unlock()`，循环 **100 次**，每次紧跟 `closeClient()`，制造最大化交叠窗口 |
| `TestFetcher_CloseWithoutClient`（[L94-L120](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L94-L120)） | 「客户端根本不存在时 Close」：恢复出的任务从未触发过懒加载、Resolve 失败的任务也没建成客户端 | 两个子测试：`restored task` 走 `FetcherManager.Restore` 的两段式构造再 `Close`；`failed resolve` 用不存在的种子路径让 `Resolve` 报错再 `Close`——都断言 `Close()` 返回 nil |
| `TestFetcher_ResolveWhileClientCloses`（[L122-L149](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L122-L149)） | 「解析途中客户端被另一协程关闭」：种子已添加但解析未完成时 `closeClient` 杀到 | **2 个 goroutine × 2 个种子文件 × 10 轮**并发执行 `Resolve → f.Close() → closeClient()`，`sync.WaitGroup` 汇合；唯一被容忍的错误是 `"torrent closed during resolution"`——那是测试自己制造的合法时序，其余任何错误都算失败 |

白盒要点：三个测试都直接操纵包级私有成员——`lock`（手动加解锁）、`closeClient()`（包级函数）、`client`（经 `initClient` 间接写）。这正是把测试放进 `bt` 包内部（而非 `bt_test` 外部包）的收益。

测试数据来自仓库自带的种子文件：`testdata/test.torrent` 与 `testdata/ubuntu-22.04-live-server-amd64.iso.torrent`（后者是真实的多文件 Ubuntu 种子，体积更大、解析路径更重，能拉长临界区内的执行时间、提高交叠概率）。

#### 4.3.4 代码实践

1. **实践目标**：用竞争检测器跑通三个竞态回归测试，并对照修复代码理解「为什么这样测」。
2. **操作步骤**：
   ```bash
   # 三个竞态回归测试（-race 开启 Go 数据竞争检测器）
   go test -race -v -run 'TestFetcher_CloseAfterInit|TestFetcher_CloseWithoutClient|TestFetcher_ResolveWhileClientCloses' ./internal/protocol/bt/
   # 对照：整个 BT 包的既有测试也一起跑一遍
   go test -race ./internal/protocol/bt/
   ```
3. **需要观察的现象**：`-v` 下三个测试名与 `TestFetcher_CloseWithoutClient` 的两个子测试名逐条打印；`-race` 若检测到数据竞争会输出 `WARNING: DATA RACE` 并以非零码失败。
4. **预期结果**：全部 PASS、无 DATA RACE 警告。这三个测试是 CI 主测试作业（`go test -tags webview …`）的常规组成部分，本地质粒度运行应当一致。若想亲眼看竞态长什么样，可 `git stash` 局部把 `addTorrentSpec` 的锁改回锁外调用 `AddTorrentSpec`（改完记得还原），再跑 `-race`——预期会看到竞争报告（**待本地验证**，竞态是否触发本身有概率性）。
5. 上述命令为标准 `go test` 用法，可本地直接验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TestFetcher_CloseAfterInit` 要循环 100 次而不是跑一次？
**答案**：竞态是概率性事件——单次「init 后立刻 close」大概率恰好不交错。循环 100 次反复制造交叠窗口，把坏时序的发生概率拉到接近必然；配合 `-race` 检测器，只要有一次真实的数据竞争就会失败。这是「以重复换确定性」的竞态测试惯用法，与 4.2 用通道精确卡点的方式互补。

**练习 2**：`TestFetcher_ResolveWhileClientCloses` 为什么唯独容忍 `"torrent closed during resolution"` 这一个错误字符串？
**答案**：测试的两个 goroutine 都在并发调 `closeClient()`，所以「种子添加后、解析完成前客户端被关闭」是测试**自己主动制造**的合法时序，由此产生的错误是预期行为而非 bug；除此之外的任何错误（如空指针、锁使用不当）都说明修复不完整。用白名单错误串划清「预期失败」与「意外失败」的边界。

**练习 3**：`initClient` 从「自己加锁」改成「必须持锁调用」，看似只是挪了一行锁，解决了什么本质问题？
**答案**：锁的**覆盖范围必须由能看见完整临界区的调用方决定**。`initClient` 自己加锁时，锁在函数返回时就释放了，调用方紧随其后的 `AddTorrentSpec` 落在锁外，形成「初始化受保护、添加裸奔」的撕裂临界区。把加锁职责上移到 `addTorrentSpec`，一个临界区才能完整罩住「初始化 + 添加」两步。这也解释了为什么契约只靠注释声明而不靠类型系统强制——Go 里这是团队约定，评审时必须检查每个调用点。

### 4.4 extmock 扩展模拟服务

#### 4.4.1 概念说明

扩展是跑在 goja/WebView 里的 JavaScript，故障常出在「脚本在这台机器的浏览器环境里行为不对」。为此项目提供了一个**手动调试 CLI**——`cmd/extmock`，它不参与 CI，是给扩展开发者用的：不启动 Flutter UI，直接在 Go 侧安装一个本地扩展文件夹、驱动 Resolve/下载、甚至对扩展引擎执行任意 JS。它是 u9 系列讲义的上手工具，也是「测试金字塔」之外的探索性工具。

#### 4.4.2 核心流程

```text
extmock resolve ./ext-dir https://example.com/file
  ├─ newExtMockDownloader：NewDownloader(MemStorage + WebViewProvider) + Setup
  ├─ InstallExtensionByFolder(dev=true)
  ├─ 可选 UpdateExtensionSettings（-setting k=v）
  ├─ d.Resolve(&base.Request{URL, Labels})
  └─ 打印 ResolveResult JSON

extmock download ./ext-dir <url> <output-dir>
  ├─ 同上解析 → 得到 Resource 文件列表
  ├─ 逐文件 d.CreateDirect(file.Req, opts)   ← 扩展产出的每文件请求
  ├─ taskTerminalWaiter 轮询 GetTask 至 done/error（5 分钟超时）
  └─ 打印 {resolve, outputDir, files[{name,path,taskId,status,size}]} 摘要

extmock eval -e "new Blob(['hello']).size"
  └─ NewExtensionEngine + runtime.Eval / EvalFile → 打印 JS 值
```

#### 4.4.3 源码精读

- 入口分派 `resolve`/`download`/`eval` 三个子命令，见 [cmd/extmock/main.go:L53-L87](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/cmd/extmock/main.go#L53-L87)；usage 里给出全部旗标与示例（[cmd/extmock/main.go:L89-L114](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/cmd/extmock/main.go#L89-L114)）。
- `runResolve`：安装 dev 扩展 → 注入设置 → `d.Resolve` 并打印结果，支持 `-label`/`-setting` 反复追加（[cmd/extmock/main.go:L116-L174](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/cmd/extmock/main.go#L116-L174)）。
- `runDownload`：对 `Res.Files` 逐个 `CreateDirect(file.Req, …)`——u2-l1 讲过 `FileInfo` 可内嵌逐文件请求，这里就是消费方；`taskTerminalWaiter` 以 50ms 间隔轮询终态（[cmd/extmock/main.go:L192-L331](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/cmd/extmock/main.go#L192-L331)，waiter 在 [L598-L639](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/cmd/extmock/main.go#L598-L639)）。
- `newExtMockDownloader` 与 WebView provider 选择：`-webview-provider go`（本机内嵌 WebView）或 `rpc`（连接外部宿主的 RPC socket，默认 `~/gopeed_webview.sock`），见 [cmd/extmock/main.go:L526-L542](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/cmd/extmock/main.go#L526-L542) 与 [L554-L588](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/cmd/extmock/main.go#L554-L588)。`go` provider 的真实实现受 `//go:build cgo` 门禁（[internal/webview/goprovider/provider.go:L1](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/webview/goprovider/provider.go#L1)），无 CGO 时落到 stub 实现（[internal/webview/goprovider/provider_stub.go:L1](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/webview/goprovider/provider_stub.go#L1)）。

#### 4.4.4 代码实践

1. **实践目标**：用 extmock 在命令行里调一段扩展 JS。
2. **操作步骤**：
   ```bash
   go build -o /tmp/extmock ./cmd/extmock
   /tmp/extmock eval -e "new Blob(['hello']).size"        # 期望输出 5
   # 若本机装好了 WebKit/GTK 依赖（Linux 上即 CI 安装的那组包），可再试：
   /tmp/extmock resolve <扩展目录> https://example.com/some-page
   ```
3. **需要观察的现象**：`eval` 打印 JS 表达式的 JSON 值；`resolve` 打印扩展改写后的 `ResolveResult`（`-pretty` 可美化）。
4. **预期结果**：`eval` 输出 `5`。`go build` 本身不需要 webview 标签即可编译（provider 有 `!cgo` stub 兜底）；但 `resolve` 走真实网页解析时需要浏览器环境，无 CGO/依赖时具体表现**待本地验证**。
5. `go build ./cmd/extmock` 为标准命令；带 WebView 的完整行为待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：extmock 的 `download` 子命令为什么对每个文件单独 `CreateDirect`，而不是 `CreateDirectBatchWithResult` 一次建完？
**答案**：extmock 的定位是**调试工具**——逐文件建任务可以对每个文件的请求、目录、状态单独观察与排错；且每个文件可能有自己的 `file.Req` 与 `file.Path`，逐个处理最贴近扩展产出的真实结构。（若追求效率确实可换成批量接口，这正是留给读者的改造练习。）

**练习 2**：`-webview-provider rpc` 与 `go` 的取舍是什么？
**答案**：`go` 在当前进程内嵌真实 WebView，简单但要求本机有 GUI 栈（CGO + WebKit/GTK）；`rpc` 通过 unix/tcp socket 连到一个已经运行着真实浏览器的宿主（例如 CI 的 macOS 作业里跑着的完整 Flutter 应用），适合在无显示器或想复用同一浏览器实例的场景。

### 4.5 Flutter 窗口导航与能力生命周期测试（d69874e/b0d4522 新增）

#### 4.5.1 概念说明

u1-l1 讲过 Flutter 前端与 Go 核心通过 FFI/gomobile/REST 三条通道对接；u11-l3 将详细讲前端结构。这里只看**前端自己怎么测**。Flutter 侧的 widget 测试（`testWidgets`）跑在 Dart VM 的测试环境里，没有真实窗口、没有原生插件、更没有 Go 后端，于是需要两层替身：

- **mock MethodChannel**：`desktop_multi_window`（桌面多窗口）、`window_manager`（窗口管理）都是原生插件，测试用 `setMockMethodCallHandler` 拦截平台通道，让原生调用返回受控值（如「创建子窗口不可用」）。
- **能力注册表桩**：UI 的一切后端调用走 `AppCapabilities`（u11-l3 的能力 RPC 体系）。测试用 `CapabilityRegistry` + `LocalCapabilityInvoker` 把 `GopeedMethods.getConfig`、`GopeedMethods.createTaskBatch` 等方法**进程内桩掉**，例如让 `createTaskBatch` 第一次抛 `StateError('offline')`、第二次返回挂起的 Future——用来测「提交失败保留输入、重试只提交一次」。

两个新测试文件分别对应本轮两个 Flutter 提交：

- `app_window_navigation_test.dart`（d69874e，351 行）：统一子表面路由 `AppRouter.openWindow` 之后，桌面开独立子窗口、Web 在应用内模态创建任务、原生移动走全页——七条测试钉住三种呈现形态的行为契约。
- `window_capability_lifecycle_test.dart`（b0d4522）：macOS 关闭子窗口后，引擎销毁不会执行 Widget.dispose，关闭的子窗口**无法主动退订**外观事件；宿主必须在感知窗口列表变化后替它清理订阅。

#### 4.5.2 核心流程

widget 测试的 `mount` 脚手架（一切用例的起点）：

```text
setUp：给三个平台通道装 mock handler
mount(tester)：
  ├─ 设定视口 1024×768、DPR=1（AGENTS.md 的桌面参考尺寸）
  ├─ CapabilityRegistry 桩：getConfig → DownloaderConfig(downloadDir: '/downloads')
  ├─ ProviderContainer(overrides: 平台/深链/通知/任务列表控制器全部换成假实现)
  ├─ pumpWidget(UncontrolledProviderScope(GopeedApp))
  └─ pumpAndSettle()   ← 等待路由与异步布局收敛
测试体：tap / enterText / 操纵 GoRouter 路由历史 / 断言 Widget 树
tearDown：AppWindowCapabilityHost.stop() + 卸载全部 mock
```

生命周期测试的验证思路（无需渲染任何 UI，纯通道层）：

```text
1. mock channels 通道：记录每次 event 投递的目标 channel 名 → deliveries
2. mock windows 通道：getAllWindows 只返回 live-child
3. host.start() 后，手动投递两个子窗口的 subscribe（closed-child、live-child）
4. 模拟 macOS 窗口关闭：投递 onWindowsChanged → 触发 _removeClosedSubscribers
5. host.updateAppearance(dark/green/zh) → 广播外观
6. 断言 deliveries == ['mixin.one/window_controller/live-child']
   （closed-child 已被清理，收不到任何投递，且全程没有 Dart 端 unsubscribe）
```

#### 4.5.3 源码精读

**① 导航测试的脚手架**。平台通道 mock 与清理见 [app_window_navigation_test.dart:L41-L56](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L41-L56)（`tearDown` 里 `AppWindowCapabilityHost.instance.stop()` 防止宿主单例跨测试泄漏）；`mount` 用 `ProviderContainer` 的 overrides 一次性桩掉五个控制器，并以 `LocalCapabilityInvoker` 接管全部后端调用，见 [app_window_navigation_test.dart:L58-L79](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L58-L79)。

**② 七条导航用例**（全部在 [app_window_navigation_test.dart](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart)）：

| 用例（行号） | 验证的行为 |
| --- | --- |
| modal cancel and history restore preserve the mounted task list（[L87-L125](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L87-L125)，variant: macOS） | 模态叠加**不销毁**底下的任务列表：模态期间 HomePage 是同一个 State 实例、搜索框文字保留；浏览器历史前后跳转能重建/关闭模态；模态尺寸恰为 820×620 |
| modal fits phone, tablet and desktop viewports（[L127-L155](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L127-L155)） | 同一 `router.go('/create')` 在手机/平板/桌面三种视口下模态都放得下（响应式布局的回归保障） |
| failed submission keeps input and retry submits once then selects downloads（[L157-L202](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L157-L202)，variant: macOS） | 桩 `createTaskBatch` 首次抛错：输入保留；重试恰好再提交一次且成功后跳转到下载列表 |
| file preview can cancel back to the modal and submit the resolved selection（[L204-L254](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L204-L254)，variant: macOS） | 种子解析后的文件预览页可退回模态，最终提交携带解析选择（`CreateTaskBatch`） |
| cancel returns to the section underneath the modal（[L256-L268](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L256-L268)） | 取消只弹出子表面，回到的仍是**打开模态前所在的分区** |
| native mobile opens a page and updates repeated share requests（[L270-L293](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L270-L293)，variant: android+iOS） | 移动端不开模态而是全页 `CreateTaskWindowPage`；分享面板重复发来第二个 URL 时**更新已开页面的输入框**而不是叠开新页 |
| logo returns home with mouse and keyboard activation（[L295-L330](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/app/router/app_window_navigation_test.dart#L295-L330)） | 主页 Logo 无论鼠标还是键盘激活都能回到首页（无障碍行为） |

**③ 被测的统一入口**。这些用例钉住的正是 d69874e 的 `AppRouter.openWindow`：桌面先试 `AppWindowLauncher.open` 独立子窗口，失败（或非桌面）则回落到应用内路由——移动 `router.go` 全页、桌面/Web `router.push` 模态；已在目标页时（移动端重复 share）只更新 `pendingCreateTaskProvider`，见 [ui/flutter/lib/app/router/app_router.dart:L40-L60](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/router/app_router.dart#L40-L60)（`openCreateTask` 是它的便捷封装，[L36-L37](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/app/router/app_router.dart#L36-L37)）。配套文档新增的「Opening Child Surfaces」一节写明了这套约定（[ui/flutter/docs/window-capability-rpc.md:L146-L171](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/docs/window-capability-rpc.md#L146-L171)）。

**④ 生命周期测试与被测的订阅清理**。测试全文见 [window_capability_lifecycle_test.dart:L21-L64](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/test/core/window/window_capability_lifecycle_test.dart#L21-L64)：`deliveries` 列表只记录投递目标，`onWindowsChanged` 模拟 macOS 窗口关闭，最后断言 `expect(deliveries, ['mixin.one/window_controller/live-child'])`——closed-child 消失了。被测代码在宿主侧：`start()` 时订阅 `onWindowsChanged`，每次窗口列表变化即触发清理（[ui/flutter/lib/core/window/window_capability_transport.dart:L63](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L63)）；`_removeClosedSubscribers` 的注释道出根因——「Engine shutdown does not run Widget.dispose, so a closed child cannot always send window.unsubscribe itself」，于是取当前窗口列表、把已不在列表中的订阅者移除（[ui/flutter/lib/core/window/window_capability_transport.dart:L74-L88](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/ui/flutter/lib/core/window/window_capability_transport.dart#L74-L88)）。

#### 4.5.4 代码实践

1. **实践目标**：本地跑通两个 Flutter 窗口测试，并读懂一条用例的替身结构。
2. **操作步骤**（在 `ui/flutter` 目录下，与 `ui/flutter/AGENTS.md` 质量门要求的本地命令一致）：
   ```bash
   cd ui/flutter
   flutter pub get
   flutter gen-l10n
   # 两个窗口测试文件
   flutter test test/app/router/app_window_navigation_test.dart
   flutter test test/core/window/window_capability_lifecycle_test.dart
   # 或全量（AGENTS.md 要求的完整质量门）
   flutter test
   ```
3. **需要观察的现象**：导航测试按用例逐条打印 PASS（带 variant 的用例每个平台各跑一遍，因此 `modal cancel…` 会以 macOS 平台标记执行）；生命周期测试只输出一条 PASS。打开 `app_window_navigation_test.dart` 对照阅读 `mount`（L58-L79）与任一用例，画出「mock 通道 → 桩能力 → 操纵路由 → 断言 Widget 树」四步。
4. **预期结果**：全部通过。注意一个**CI 现状**：`.github/workflows/test.yml` 的两个 Flutter 作业目前只显式挑选了 `test/api/downloader_config_test.dart`、task_context_menu/app_text_field 与 widget_test 的一个子集（见 4.6），这两个窗口测试文件**不在任何 CI 作业的显式命令里**——它们靠本地 `flutter test` 全量（AGENTS.md 质量门）与 PR 评审保证，这是如实陈述的事实，读者贡献 UI 改动时务必本地全量跑。
5. `flutter test` 为标准命令，可本地直接验证（需先装好 Flutter 3.41.x 工具链）。

#### 4.5.5 小练习与答案

**练习 1**：`mount` 为什么用 `LocalCapabilityInvoker(registry)` 而不是走真实的 api.dart 调用？
**答案**：widget 测试跑在纯 Dart VM 里，没有 FFI 绑定也没有 REST 服务器，真实的 `api.dart` 无处可去。能力注册表把 `GopeedMethods.getConfig` 等方法在进程内换成静态返回值（甚至可编程的「第一次抛错、第二次挂起」），UI 层因此完全离线可测，且能精确模拟后端故障——这正是 u11-l3 将讲到的能力 RPC 体系带来的可测性红利。

**练习 2**：`window_capability_lifecycle_test` 如何证明「关闭的子窗口不再收到投递」？它连一个 Widget 都没渲染，凭什么有效？
**答案**：它测的是**通道层协议**而非 UI：`deliveries` 列表经 mock 的 channels 通道记录每次 event 投递的目标；先让 closed-child 与 live-child 都订阅成功，再投递 `onWindowsChanged` 触发宿主清理，最后 `updateAppearance` 广播——若清理失效，closed-child 的 channel 也会出现在 deliveries 里。断言 `deliveries == ['mixin.one/window_controller/live-child']` 一锤定音。不需要渲染 UI，因为订阅表就长在 `AppWindowCapabilityHost` 这个单例上。

**练习 3**：多数导航用例标 `variant: TargetPlatformVariant({TargetPlatform.macOS})`，而「native mobile opens a page…」标 `{android, iOS}`，为什么同一份代码要分平台各跑？
**答案**：`AppRouter.openWindow` 会按平台选择呈现形态——桌面/浏览器走应用内模态（`AppWindowPage`），原生移动走全页路由。`variant` 让同一测试在不同 `TargetPlatform` 下各执行一遍，分别命中不同分支；macOS 变体断言模态行为（820×620、历史恢复），android/iOS 变体断言全页与重复 share 更新。反过来，如果把模态用例放到 android 变体下跑，断言 `app-window-modal` 存在就会失败。

### 4.6 CI 命令矩阵与覆盖率采集

#### 4.6.1 概念说明

[.github/workflows/test.yml](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml) 是全部质量闸门。它由一个核心测试作业（含覆盖率上传）、两个 Flutter 侧测试作业、一个三 OS webview 集成矩阵和四个构建检查作业组成；PR 与 push 到 main 都只在 `bind/cmd/internal/pkg/ui` 等路径变化时触发（[.github/workflows/test.yml:L3-L28](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L3-L28)），避免文档改动浪费流水线。工具链版本固定为 Go 1.25.4 / Flutter 3.41.6（[L30-L32](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L30-L32)，与 [go.mod:L3](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/go.mod#L3) 对齐）。

#### 4.6.2 核心流程

核心测试作业的命令链（[.github/workflows/test.yml:L48-L71](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L48-L71)）：

```bash
# 1. 选包：排除 bind/（cgo 绑定无测试）与 cmd/*，但保留 cmd/web（有 flags_test）
PACKAGES=$(go list ./... | awk '!/\/bind\// && (!/\/cmd\// || /\/cmd\/web$/)')
# 2. 跑测：CGO 开启（bbolt/webview 需要）+ 虚拟显示 + webview 标签 + 跨包覆盖率
env CGO_ENABLED=1 xvfb-run -a go test -tags webview -v \
  -coverpkg=$PACKAGES -covermode=count -coverprofile=coverage.txt $PACKAGES
# 3. 过滤 main.go 行（入口函数拉低覆盖率）后上传 codecov
grep -v "main.go" coverage.txt > coverage_filtered.txt || true
```

注意：4.3 的三个 BT 竞态回归测试就在这条命令的覆盖范围内（属于 `internal/protocol/bt` 包）；而 4.5 的两个 Flutter 窗口测试文件不在其中——CI 的 Flutter 作业只显式挑选了部分测试文件（见 4.5.4 的说明）。

#### 4.6.3 源码精读

- **包选择 awk**：一条表达式完成「排除法 + 例外」，注释明确说明保留 `cmd/web` 是为了它的 flags 测试（[.github/workflows/test.yml:L60-L64](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L60-L64)）。
- **`-tags webview` + `xvfb-run`**：让带 `//go:build cgo && webview` 门禁的集成测试（如 [internal/webview/goprovider/provider_integration_test.go:L1-L21](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/webview/goprovider/provider_integration_test.go#L1-L21)，跑 Provider 契约/Profile/事件三组合同测试）参与编译，并在无显示器的 Linux 上给 GTK 事件循环一个虚拟 X server。
- **webview 集成矩阵**（[.github/workflows/test.yml:L115-L202](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L115-L202)）：Windows/Linux/macOS 三系统策略各不相同——Linux 先 `go test -c` 编出测试二进制再以 `-test.count=5` 在**同一 GTK 事件循环**上重复跑，专捕「引擎销毁后残留回调」；Windows `-count=3` 捕浏览器关闭竞态；macOS 最重：真编 `libgopeed.dylib`、`flutter run --machine` 拉起完整应用并监听 `app.started` 事件，再让 rpcprovider 测试通过 unix socket 连进这个活应用（[L158-L202](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L158-L202)）。
- **共享规则表双端跑**：`test-flutter-category` 作业跑 `ui/flutter/test/api/downloader_config_test.dart`，注释点明分类规则表由设置界面与后端共同读取，Go 侧已由主测试作业覆盖、Dart 侧由本作业覆盖（[.github/workflows/test.yml:L72-L88](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L72-L88)）。另一个 Flutter 作业 `test-flutter-task-context-menu` 只挑了 task_context_menu/app_text_field 两个文件与 widget_test 的一个 `--plain-name` 子集，且在 VM 与 Chrome 双平台各跑一轮（[.github/workflows/test.yml:L89-L114](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L89-L114)）。
- **构建检查**：desktop 五 OS 矩阵（含 Windows ARM64 与 Ubuntu ARM）、mobile（macOS 出 iOS、Linux 出 Android）、web（Flutter Web 产物嵌入 `cmd/web/dist` 后 `go build -tags nosqlite,web`）、docker，全部 `needs: [test]` 串联（[.github/workflows/test.yml:L204-L347](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L204-L347)）。lint 作业目前整段注释掉（[L35-L47](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/.github/workflows/test.yml#L35-L47)）。

#### 4.6.4 代码实践

1. **实践目标**：在本地复现 CI 的选包与测试命令。
2. **操作步骤**：
   ```bash
   # 只看 CI 会测试哪些包（不动任何东西）
   go list ./... | awk '!/\/bind\// && (!/\/cmd\// || /\/cmd\/web$/)'
   # 无 GTK 依赖时的简化版（去掉 webview 标签与 xvfb；CGO 保留，bbolt 需要）
   go test ./pkg/download/... ./pkg/api/... ./pkg/rest/... ./internal/protocol/bt/...
   ```
3. **需要观察的现象**：第一条命令输出的包列表里没有 `bind/…`、`cmd/gopeed`，但有 `cmd/web`；测试会真实下载（假服务器）并打印各测试结果。
4. **预期结果**：包列表符合排除规则；测试全绿。Linux 上若已安装 `libgtk-3-dev libwebkit2gtk-4.1-dev xvfb`，则可完整复现 CI 命令（含 `-tags webview` 与 `xvfb-run`）；未安装时带 webview 标签的编译会失败，属预期。
5. `go list`/`go test` 为标准命令，可本地直接验证；完整 CI 等价命令待本地验证（取决于本机 GUI 依赖）。

#### 4.6.5 小练习与答案

**练习 1**：为什么 CI 用 `-coverpkg=$PACKAGES` 指定全部包，而不是让每个包只统计自己的覆盖率？
**答案**：`-coverpkg` 把指定包的插桩打进**被测进程**，跨包测试（如 `pkg/rest` 的测试穿过 api 层直达 download 层）也能给沿途代码记覆盖率，总量远大于各包自测之和，得到的 coverage.txt 才真实反映整条调用链的覆盖情况。

**练习 2**：Linux webview 作业为什么要 `go test -c` 手动编二进制、再 `-test.count=5` 重复执行？
**答案**：注释写明目标是「在同一 GTK 事件循环上重复，捕获引擎销毁后仍在排队的回调」。`-count=5` 让同一进程内五轮测试共享进程级状态（GTK 主循环、全局注册表），单次运行看似正常的销毁时序 bug 在重复后才会暴露；先 `-c` 是为了带上与主测试一致的 `-coverpkg` 插桩。

**练习 3**：如果只想在本地快速验证自己改动的 `pkg/download` 代码没破坏分组功能，最小命令是什么？
**答案**：`go test -run 'TestDownloader_TaskGroup|TestDownloader_Batch' ./pkg/download/`——正则同时匹配两组测试，跳过耗时的真实 HTTP 下载用例（那些用例生成 200MB 测试文件），几十秒内出结果。

## 5. 综合实践

**为 u3-l4 的自定义 `mock://` 协议补一组单元测试**，把本讲三层替身手法全部用一遍。在 `pkg/download/` 下新建 `mock_protocol_test.go`（示例代码，仅供练习）：

1. **Resolve 分支**：仿照 `generationTestFetcher`（[pkg/download/downloader_test.go:L128-L161](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L128-L161)），构造 `mock://ok` 与 `mock://bad` 两种 URL，断言前者 Resolve 成功返回单文件资源、后者返回错误且 Task 不产生。
2. **断点续传分支**：仿照 `recoveryTestManager`（[pkg/download/downloader_test.go:L191-L217](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L191-L217)）为协议实现 Store/Restore，写出「下载→暂停→Close→新 Downloader 重开同目录→Continue→完成」的恢复测试，参考 `TestDownloader_StoreAndRestore`（[pkg/download/downloader_test.go:L1195](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go#L1195)）的骨架。
3. **错误分支**：给 Fetcher 加一个 `done <- err` 的出错开关，断言任务进入 error 态且 `Wait()` 的错误被 watch 循环正确写回 `task.Error`。
4. **（进阶）竞态分支**：对照 `TestFetcher_ResolveWhileClientCloses`（[internal/protocol/bt/fetcher_test.go:L122-L149](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L122-L149)）的手法，起两个 goroutine 并发对同一个 `mock://` 任务做 Resolve/Pause/Close 循环各 20 轮，`go test -race` 下全绿。想一想：你的协议里有没有类似 BT `lock` 的共享状态？如果没有，这个测试验证的是什么（提示：Downloader 侧的 runGeneration 与状态机）。
5. **验证**：`go test -race -run 'TestMockProtocol' -v ./pkg/download/`，四条路径全绿即完成。若你的 u3-l4 实现与本仓库测试脚手架字段有出入，以让测试通过为准调整练习代码（不要改产品源码）。

## 6. 本讲小结

- **`internal/test` 假服务器家族**：一台服务器模拟一种线上故障（一次性 URL、忽略 Range、过期重定向、Cookie 失效、连接数限制、多给数据……），`rangeFileHandle` 是可注入违规行为的 Range 处理核心，200MB 随机数据 + 随机端口 + 自动清理保证隔离与确定性。
- **下载器级测试靠内存协议**：`generationTestManager`（holdOpen 卡运行态、通道卡竞态点、原子计数）+ 存储装饰器（按桶注入失败）+ 并发卡点装饰器（entered/release 通道），让最难的竞态与故障恢复变成确定性断言；上一轮的约 1100 行分组/批量测试直接为 u4-l4/u4-l6 的语义背书。
- **BT 竞态回归测试（cd19772）**：共享客户端的「构建 vs 关闭」竞态用「以重复换确定性」的方式钉住——100 次 init 即关、双协程 10 轮解析+关闭；修复的关键是把锁窗口从 `initClient` 延伸到 `addTorrentSpec` 全程、closeCtx 以参数传入协程；白名单错误串划清「测试自造的合法失败」与「意外失败」。
- **Flutter 窗口测试（d69874e/b0d4522）**：mock MethodChannel 替原生插件、`CapabilityRegistry` + `LocalCapabilityInvoker` 桩 Go 后端、`variant` 分平台命中不同呈现分支；生命周期测试不渲染任何 Widget，纯靠通道层投递记录验证「关闭的引擎失去订阅」。
- **extmock 是探索工具不是测试**：不进 CI，供扩展开发者命令行驱动 Resolve/下载/eval JS，`go`/`rpc` 两种 WebView provider 分别适配本机与外部宿主。
- **CI 命令矩阵**：排除法选包 + `-tags webview` + `xvfb-run` + 跨包 `-coverpkg` 覆盖率 + codecov；webview 集成在三 OS 上用三种策略重复执行捕销毁竞态；两个 Flutter 作业只显式挑选部分测试文件，窗口导航/生命周期测试目前依赖本地 `flutter test` 全量保障；四个构建检查作业在测试通过后串联。

## 7. 下一步学习建议

- 下一讲 **u11-l2 并发控制与优雅关闭**：本讲多次出现的 `waitForTaskStatus`、`runGeneration`、通道卡点与 BT 的「持锁窗口」，都将在锁清单与 Close 时序的框架下得到统一解释。
- 下一讲 **u11-l3 Flutter 前端如何对接 Go 核心**：本讲 4.5 只是前端测试的切面，完整的能力 RPC、三端通信策略与统一子表面路由将在那里展开；测试里桩掉的 `GopeedMethods.*` 正是那条调用链的入口。
- 建议继续精读 [pkg/download/downloader_test.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader_test.go) 中的 `TestDownloader_StaleStartGenerationCannotRestartTask`（L577）与 `TestDownloader_ConcurrentResolvedCreation`（L4916），它们是竞态测试的进阶范本；BT 侧再读 [internal/protocol/bt/fetcher_test.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go) 的 `TestFetcher_Patch`（L324），看协议级测试如何组装真实种子。
- 若对 webview 集成感兴趣，读 [internal/webview/integrationtest/contract.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/webview/integrationtest/contract.go) 看三种 provider 如何用同一份「合同测试」约束行为一致。
