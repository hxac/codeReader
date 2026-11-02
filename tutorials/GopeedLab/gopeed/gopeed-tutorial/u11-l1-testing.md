# 测试体系与 CI

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 Gopeed 各层测试（协议级、下载器级、服务层、REST 层、webview 集成）分别放在哪里、怎么组织、用什么测试替身。
2. 理解 `internal/test` 的 HTTP 假服务器家族——每台「坏服务器」模拟哪一种线上故障。
3. 读懂本轮提交（1e36599）新增的约 1100 行批量/分组任务测试：它们如何用内存协议、存储装饰器和通道同步复现竞态与故障。
4. 会用 `cmd/extmock` 在不启动 Flutter UI 的情况下调试扩展。
5. 能在本地复现 CI 的测试命令，理解 `-tags webview`、`xvfb-run`、`-coverpkg` 与多 OS 矩阵各自解决什么问题。

## 2. 前置知识

- **测试替身（test double）**：真实网络、真实磁盘、真实浏览器在测试里既慢又不可复现。替身是用受控实现替换真实依赖的艺术：假 HTTP 服务器（替网络）、内存 Fetcher（替协议）、存储装饰器（替磁盘故障）、无头 WebView（替浏览器）。
- **表驱动测试**：Go 惯用法，把「输入 + 期望输出」列成结构体切片循环执行，一个函数覆盖十几种边界。本讲的分组目录白名单测试就是典型。
- **白盒测试**：`pkg/download/downloader_test.go` 与被测代码在**同一个包**里，测试可以直接访问私有字段（如 `d.groups`、`d.waitTasks`、`d.fetcherCache`），这是它能精确断言内部状态的原因。
- **通道同步（channel synchronization）**：要测试「删除进行到一半时另一个删除并发到达」这类竞态，测试用 `entered`/`release` 一对通道把被测代码精确卡在临界区中间，比 `time.Sleep` 可靠得多。
- **build tags 与 xvfb**：`-tags webview` 会编译依赖 WebKit/GTK 的真实 WebView 代码（需要 CGO）；`xvfb-run` 在无显示器的 CI 机器上提供虚拟 X 显示。二者配合才能在 Linux 云端跑浏览器级测试。
- 前置讲义：u3-l4（自定义协议 Fetcher，本讲综合实践为它补测试）、u4-l2（任务主链路）、u4-l6（TaskGroup 与批量创建，本轮新测试的主角）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `internal/test/httptest.go` | HTTP 假服务器家族：慢速、限连、忽略 Range、一次性 URL、过期重定向、Cookie 过期等十余种「坏服务器」 |
| `internal/test/util.go` | 校验工具：`FileMd5`/`DirMd5`（完整性断言）、`ToJson`/`JsonEqual` |
| `pkg/download/downloader_test.go` | 下载器级测试主战场（5000+ 行）：`generationTestManager` 测试协议、blob、调度竞态、批量/分组任务 |
| `pkg/download/category_test.go` | 自动分类与目录决策的表驱动单测（含新增分组白名单测试） |
| `pkg/download/extension_test.go` | 扩展测试 + 共享辅助：`setupDownloader`、`waitForTaskStatus` |
| `pkg/api/service_test.go` | 服务层测试：不启动 HTTP 服务器，直接 `Service.JSON` 进程内调用 |
| `pkg/rest/server_test.go` | REST 层测试：真实 HTTP 往返 + Mem/Bolt 双存储矩阵 |
| `cmd/extmock/main.go` | 扩展模拟调试 CLI：resolve/download/eval 三个子命令 |
| `.github/workflows/test.yml` | CI：单元测试 + 覆盖率 + Flutter 侧测试 + 三 OS webview 集成 + 四类构建检查 |
| `internal/webview/goprovider/provider_integration_test.go` | webview 集成测试入口（`-tags webview` 门禁） |

## 4. 核心概念与源码讲解

### 4.1 internal/test 辅助：可控的 HTTP 假服务器

#### 4.1.1 概念说明

HTTP 下载器的绝大多数 bug 都发生在「服务器行为不理想」时：Range 被忽略、重定向过期、Cookie 中途失效、连接数被限……这些场景没法靠一个正常服务器测出来。`internal/test/httptest.go` 的思路是：**每一种线上故障，造一台专门的假服务器**。它放在 `internal/` 下，意味着只有本项目测试能引用，不对外承诺 API 稳定。

所有服务器共享一份测试资产：常量 `BuildName`/`BuildSize`（200MB 的 `build.data`），见 [internal/test/httptest.go:L24-L43](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L24-L43)。注意 `BuildSize = 200 * 1024 * 1024`——每启动一台服务器都会生成一个 200MB 随机文件，这是完整测试套件较重的原因之一。

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

**公共底座 `startTestServer`**：随机端口 + 随机数据 + 自清理，见 [internal/test/httptest.go:L836-L871](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L836-L871)。`shutdownListener.Close()` 先优雅关停再删三个产物文件，保证测试之间互不污染（[internal/test/httptest.go:L879-L893](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L879-L893)）。

**Range 请求处理核心 `rangeFileHandle`**：手工解析 `Range: bytes=start-end`、Seek 到起点、设置 `Content-Range`/`Content-Length` 并返回 206，见 [internal/test/httptest.go:L705-L779](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L705-L779)。它的 `modifyEnd` 钩子允许服务器「故意多给数据」——`StartTestRangeBugServer` 用它制造 `Range: bytes=0-100` 却返回 150 字节的违规响应（[internal/test/httptest.go:L679-L703](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L679-L703)），正好对应 u5-l3 讲过的 Range 完整性校验。

**坏服务器家族速查表**（都在 [internal/test/httptest.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go) 中）：

| 服务器 | 模拟的故障 | 位置 |
| --- | --- | --- |
| `StartTestOneTimeServer` | 一次性签名 URL：首个请求成功、后续 404（`atomic.Bool` 保证并发下也只放行一次） | [L313-L336](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L313-L336) |
| `StartTestNoRangeSlowServer` | 不支持 Range 的慢速服务器 | [L340-L373](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L340-L373) |
| `StartTestIgnoredRangeServer` | 声称支持 Range（`Accept-Ranges: bytes`）却恒返 200 全量 | [L377-L412](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L377-L412) |
| `StartTestExpiringRedirectServer` | 重定向 URL 会过期（403），逼客户端回原 URL 重新拿新重定向 | [L421-L473](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L421-L473) |
| `StartTestConLimitServer` | 并发连接数超限返回 403（慢启动控制器的收缩测试） | [L526-L551](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L526-L551) |
| `StartTestFailThenRecoverServer` | 先连续失败后恢复；用 **416** 而非 500，因为 5xx 豁免于失败计数（u5-l3 讲过该规则） | [L634-L675](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L634-L675) |
| `StartTestPatchURLServer` | `/bad-url` 恒 404 + `/good-url` 正常，测「换 URL 续传」 | [L928-L949](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L928-L949) |
| `StartTestCookieExpiringServer` | Cookie 中途过期（401），测「换 Cookie 续传」 | [L956-L1004](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L956-L1004) |
| `StartSocks5Server` | 本地 SOCKS5 代理（可带账号密码），测代理解析 | [L902-L921](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L902-L921) |
| `StartTestCustomServer` | 文件名解析大礼包：`Content-Disposition`、RFC 5987 `filename*`、GBK 编码、HTML 实体 `&amp;`、加号编码、403 等 | [L70-L233](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L70-L233) |

**`internal/test/util.go`** 提供「下载是否真的对」的最终裁决：`FileMd5` 对比源文件与产物（[internal/test/util.go:L12-L28](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/util.go#L12-L28)），`AssertResourceEqual` 在忽略 ctime 后深比较 Resource（[internal/test/httptest.go:L1006-L1012](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/test/httptest.go#L1006-L1012)）。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到假服务器的「启动—服务—自清理」闭环。
2. **操作步骤**：运行只依赖文件服务器的单个测试，并用 `-v` 观察输出：
   ```bash
   go test -v -run 'TestDownloader_CreateDirectBatch$' ./pkg/download/
   ```
   然后运行 `TestDownloader_Resolve`（[pkg/download/downloader_test.go:L241-L272](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L241-L272)），它在 `StartTestCustomServer` 上请求 `disposition`、`gbk-encoded` 等端点。
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

假 HTTP 服务器替的是**网络**，但很多下载器行为（排队、分组、删除恢复、竞态）根本不需要走网络——它们需要一个**假协议**。`generationTestManager` 就是注册进 Downloader 的最小 FetcherManager（u3-l4 的 `mock://` 协议正是参考它实现的）。本轮提交（1e36599）在 `pkg/download/downloader_test.go` 新增了 28 个测试函数、约 1100 行，全部建立在这套替身之上，覆盖 u4-l6 讲过的 TaskGroup 分组与 `CreateDirectBatchWithResult` 批量语义。

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

**① `generationTestManager`/`generationTestFetcher`：内存协议**。管理器只按 `generation://` 前缀匹配（[pkg/download/downloader_test.go:L110-L126](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L110-L126)）；Fetcher 的 `Resolve` 返回 1 字节单文件资源、`Start` 在 `holdOpen=false` 时立刻向 `done` 通道发 nil（[pkg/download/downloader_test.go:L128-L161](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L128-L161)）。配套的 `recoveryTestManager` 补上 `Store`/`Restore`，专门服务断点恢复类测试（[pkg/download/downloader_test.go:L191-L217](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L191-L217)）。

**② 分组测试脚手架**。`newTaskGroupDownloader` 一步造出「单协议 + 内存存储 + holdOpen」的 Downloader，并直接改写私有字段 `d.cfg.MaxRunning`（白盒优势），见 [pkg/download/downloader_test.go:L4048-L4060](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4048-L4060)；`taskGroupBatch` 生成 `Group:true, GroupName:"folder"` 加 N 个 `generation://file/i` 成员的批量请求（[pkg/download/downloader_test.go:L4062-L4068](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4062-L4068)）。

**③ 存储装饰器**。`taskGroupTestStorage` 嵌入 `Storage` 接口，按桶名拦截：`failGroupPut` 让 `task_group` 桶写入失败，`beforePut` 让普通任务落盘前注入错误（[pkg/download/downloader_test.go:L4161-L4184](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4161-L4184)）。有了它，`TestDownloader_TaskGroupPartialFailureAndFirstSave` 才能让 3 个成员中间那个写盘失败、验证「部分成功」——批量为 3 只建成 2 个，且**首次落盘就带 ParentID**（[pkg/download/downloader_test.go:L4186-L4224](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4186-L4224)）。

**④ 分组元数据与重启恢复**。`TestDownloader_TaskGroupMetadataAndRestore` 用真 `BoltStorage` 验证三件事：组不是任务（`GetTask(parentID) == nil`）；成员任务的 JSON 序列化含 `"group":` 但不含 `"type":`（组不会混进任务列表）；`Close()` 后重开同目录，`task.group` 指针被 Setup 重新挂接、删除组 ID 连带清空 `d.groups`（[pkg/download/downloader_test.go:L4070-L4110](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4070-L4110)）。这正对应 u4-l6 讲的「ParentID 落盘为权威、group 指针为运行时缓存」双轨制。

**⑤ 队列与按组操作**。`TestDownloader_TaskGroupQueue` 以 `MaxRunning=5` 提交 6 成员批（叠加 0/2 个既有任务），断言 `running=5-existing`、`waiting=1+existing`；再按「组 ID + 成员 ID」暂停，验证全部成员进入 pause 且 `d.waitTasks` 清空、`expandGroupTaskIDs` 去重后仍为 6（[pkg/download/downloader_test.go:L4112-L4159](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4112-L4159)）。

**⑥ 删除失败的持久化恢复**（u4-l4 rebuild 讲义的核心机制的测试证据）：

- `TestDownloader_TaskGroupDeletePersistence`：删除成员失败后，任务**必须仍在内存**且**必须仍在 `bucketTask` 桶里**——测试直接 `storage.Get(bucketTask, …)` 验证落盘恢复（[pkg/download/downloader_test.go:L4367-L4385](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4367-L4385)）。
- `TestDownloader_TaskGroupConcurrentDelete`：用 `entered`/`release` 通道把成员 A 的删除卡在存储层，此刻删掉成员 B——断言组元数据**不被并发清理误删**（[pkg/download/downloader_test.go:L4387-L4426](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4387-L4426)）。
- `TestDownloader_TaskGroupDeleteFailureRestoresQueue`：删除一个 wait 态成员失败后，它必须回到 `d.waitTasks` 队列；随后删掉 running 成员，被保留者应被唤醒进入 running（用共享辅助 `waitForTaskStatus` 轮询断言，[pkg/download/downloader_test.go:L4479-L4501](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4479-L4501)；该辅助定义于 [pkg/download/extension_test.go:L2042-L2063](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/extension_test.go#L2042-L2063)，done 态还要等 `watchedTasks` 登记清除，避免竞态误判）。

**⑦ 批量三规则的直接验证**。u4-l6 讲过「rid 优先、成员 opts 整体替换、部分成功」，`TestDownloader_BatchMixedResolvedAndDirect` 把它们压进一个批次：`Rid:"missing"`（跳过）、`Rid+Req 并存`（rid 赢，URL 是 `unsupported://` 仍按已解析资源建任务）+ `SelectFiles:[1]`（选择保留）、纯 `Req` 直建、空成员（跳过）；再断言成功的 rid 已从 `fetcherCache` 消费、失败的 rid 不阻断后续合法成员（[pkg/download/downloader_test.go:L4801-L4842](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4801-L4842)）。全败路径由 `TestDownloader_BatchAllFailedAndResolvedSelection` 覆盖：报错、rid **不被**消费、空组与预留目录全部回收（[pkg/download/downloader_test.go:L4844-L4869](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4844-L4869)）；「启动失败但已落盘的任务算成功且保留成员资格」由 `TestDownloader_BatchStartFailureKeepsCreatedTasks` 覆盖（[pkg/download/downloader_test.go:L4871-L4894](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4871-L4894)）。

**⑧ 上层测试如何接力**。三层测试各自换一种「驱动方式」验证同一批语义：

- **服务层**（不启 HTTP 服务器）：`TestBatchResolvedAndDirectCreation` 用泛型辅助 `serviceTestRequest` 直接调 `Service.JSON`，循环 `grouped=false/true` 断言响应字段——非分组只有 `taskIds`、分组多出 `parentId`；全败批次返回错误信封（[pkg/api/service_test.go:L271-L331](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L271-L331)，辅助在 [L71-L90](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service_test.go#L71-L90)）。
- **REST 层**（真实 HTTP 往返）：`TestCreateDirectTaskBatchWithGroup` 断言响应类型已从 ID 数组改为 `CreateTaskBatchResult`，且任务列表只含真实任务、每个成员带 `parentId` 与 `group` 元数据（[pkg/rest/server_test.go:L250-L282](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server_test.go#L250-L282)）。注意 `doTest0` 会让**同一个测试跑两遍**——`StorageMem` 与 `StorageBolt`（[pkg/rest/server_test.go:L1207-L1260](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/rest/server_test.go#L1207-L1260)），存储矩阵内建在测试里。
- **分类目录**（纯单测、无 Downloader Setup）：新增的 `TestDecideGroupDownloadDirWhiteList` 是表驱动范本——组根/嵌套成员放行、组外成员与权限被收回的嵌套路径拒绝、字面量 `%year%` 组名不展开；`newUnitCategoryDownloader` 只挂配置不跑 Setup（[pkg/download/category_test.go:L237-L268](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L237-L268)，脚手架在 [L19-L51](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L19-L51)）。

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

### 4.3 extmock 扩展模拟服务

#### 4.3.1 概念说明

扩展是跑在 goja/WebView 里的 JavaScript，故障常出在「脚本在这台机器的浏览器环境里行为不对」。为此项目提供了一个**手动调试 CLI**——`cmd/extmock`，它不参与 CI，是给扩展开发者用的：不启动 Flutter UI，直接在 Go 侧安装一个本地扩展文件夹、驱动 Resolve/下载、甚至对扩展引擎执行任意 JS。它是 u9 系列讲义的上手工具，也是「测试金字塔」之外的探索性工具。

#### 4.3.2 核心流程

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

#### 4.3.3 源码精读

- 入口分派 `resolve`/`download`/`eval` 三个子命令，见 [cmd/extmock/main.go:L53-L87](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/extmock/main.go#L53-L87)；usage 里给出全部旗标与示例（[cmd/extmock/main.go:L89-L114](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/extmock/main.go#L89-L114)）。
- `runResolve`：安装 dev 扩展 → 注入设置 → `d.Resolve` 并打印结果，支持 `-label`/`-setting` 反复追加（[cmd/extmock/main.go:L116-L174](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/extmock/main.go#L116-L174)）。
- `runDownload`：对 `Res.Files` 逐个 `CreateDirect(file.Req, …)`——u2-l1 讲过 `FileInfo` 可内嵌逐文件请求，这里就是消费方；`taskTerminalWaiter` 以 50ms 间隔轮询终态（[cmd/extmock/main.go:L192-L331](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/extmock/main.go#L192-L331)，waiter 在 [L598-L639](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/extmock/main.go#L598-L639)）。
- `newExtMockDownloader` 与 WebView provider 选择：`-webview-provider go`（本机内嵌 WebView）或 `rpc`（连接外部宿主的 RPC socket，默认 `~/gopeed_webview.sock`），见 [cmd/extmock/main.go:L526-L542](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/extmock/main.go#L526-L542) 与 [L554-L588](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/extmock/main.go#L554-L588)。`go` provider 的真实实现受 `//go:build cgo` 门禁（[internal/webview/goprovider/provider.go:L1](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/webview/goprovider/provider.go#L1)），无 CGO 时落到 stub 实现（[internal/webview/goprovider/provider_stub.go:L1](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/webview/goprovider/provider_stub.go#L1)）。

#### 4.3.4 代码实践

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

#### 4.3.5 小练习与答案

**练习 1**：extmock 的 `download` 子命令为什么对每个文件单独 `CreateDirect`，而不是 `CreateDirectBatchWithResult` 一次建完？
**答案**：extmock 的定位是**调试工具**——逐文件建任务可以对每个文件的请求、目录、状态单独观察与排错；且每个文件可能有自己的 `file.Req` 与 `file.Path`，逐个处理最贴近扩展产出的真实结构。（若追求效率确实可换成批量接口，这正是留给读者的改造练习。）

**练习 2**：`-webview-provider rpc` 与 `go` 的取舍是什么？
**答案**：`go` 在当前进程内嵌真实 WebView，简单但要求本机有 GUI 栈（CGO + WebKit/GTK）；`rpc` 通过 unix/tcp socket 连到一个已经运行着真实浏览器的宿主（例如 CI 的 macOS 作业里跑着的完整 Flutter 应用），适合在无显示器或想复用同一浏览器实例的场景。

### 4.4 CI 命令矩阵与覆盖率采集

#### 4.4.1 概念说明

[.github/workflows/test.yml](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml) 是全部质量闸门。它由一个核心测试作业（含覆盖率上传）、两个 Flutter 侧测试作业、一个三 OS webview 集成矩阵和四个构建检查作业组成；PR 与 push 到 main 都只在 `bind/cmd/internal/pkg/ui` 等路径变化时触发（[.github/workflows/test.yml:L3-L28](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml#L3-L28)），避免文档改动浪费流水线。工具链版本固定为 Go 1.25.4 / Flutter 3.41.6（[L30-L32](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml#L30-L32)，与 [go.mod:L3](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/go.mod#L3) 对齐）。

#### 4.4.2 核心流程

核心测试作业的命令链（[.github/workflows/test.yml:L48-L71](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml#L48-L71)）：

```bash
# 1. 选包：排除 bind/（cgo 绑定无测试）与 cmd/*，但保留 cmd/web（有 flags_test）
PACKAGES=$(go list ./... | awk '!/\/bind\// && (!/\/cmd\// || /\/cmd\/web$/)')
# 2. 跑测：CGO 开启（bbolt/webview 需要）+ 虚拟显示 + webview 标签 + 跨包覆盖率
env CGO_ENABLED=1 xvfb-run -a go test -tags webview -v \
  -coverpkg=$PACKAGES -covermode=count -coverprofile=coverage.txt $PACKAGES
# 3. 过滤 main.go 行（入口函数拉低覆盖率）后上传 codecov
grep -v "main.go" coverage.txt > coverage_filtered.txt || true
```

#### 4.4.3 源码精读

- **包选择 awk**：一条表达式完成「排除法 + 例外」，注释明确说明保留 `cmd/web` 是为了它的 flags 测试（[.github/workflows/test.yml:L60-L64](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml#L60-L64)）。
- **`-tags webview` + `xvfb-run`**：让带 `//go:build cgo && webview` 门禁的集成测试（如 [internal/webview/goprovider/provider_integration_test.go:L1-L21](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/webview/goprovider/provider_integration_test.go#L1-L21)，跑 Provider 契约/Profile/事件三组合同测试）参与编译，并在无显示器的 Linux 上给 GTK 事件循环一个虚拟 X server。
- **webview 集成矩阵**（[.github/workflows/test.yml:L115-L202](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml#L115-L202)）：Windows/Linux/macOS 三系统策略各不相同——Linux 先 `go test -c` 编出测试二进制再以 `-test.count=5` 在**同一 GTK 事件循环**上重复跑，专捕「引擎销毁后残留回调」；Windows `-count=3` 捕浏览器关闭竞态；macOS 最重：真编 `libgopeed.dylib`、`flutter run --machine` 拉起完整应用并监听 `app.started` 事件，再让 rpcprovider 测试通过 unix socket 连进这个活应用（[L158-L202](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml#L158-L202)）。
- **共享规则表双端跑**：`test-flutter-category` 作业跑 `ui/flutter/test/api/downloader_config_test.dart`，注释点明分类规则表由设置界面与后端共同读取，Go 侧已由主测试作业覆盖、Dart 侧由本作业覆盖（[.github/workflows/test.yml:L72-L88](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml#L72-L88)）。
- **构建检查**：desktop 五 OS 矩阵（含 Windows ARM64 与 Ubuntu ARM）、mobile（macOS 出 iOS、Linux 出 Android）、web（Flutter Web 产物嵌入 `cmd/web/dist` 后 `go build -tags nosqlite,web`）、docker，全部 `needs: [test]` 串联（[.github/workflows/test.yml:L204-L347](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml#L204-L347)）。lint 作业目前整段注释掉（[L35-L47](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/.github/workflows/test.yml#L35-L47)）。

#### 4.4.4 代码实践

1. **实践目标**：在本地复现 CI 的选包与测试命令。
2. **操作步骤**：
   ```bash
   # 只看 CI 会测试哪些包（不动任何东西）
   go list ./... | awk '!/\/bind\// && (!/\/cmd\// || /\/cmd\/web$/)'
   # 无 GTK 依赖时的简化版（去掉 webview 标签与 xvfb；CGO 保留，bbolt 需要）
   go test ./pkg/download/... ./pkg/api/... ./pkg/rest/...
   ```
3. **需要观察的现象**：第一条命令输出的包列表里没有 `bind/…`、`cmd/gopeed`，但有 `cmd/web`；测试会真实下载（假服务器）并打印各测试结果。
4. **预期结果**：包列表符合排除规则；测试全绿。Linux 上若已安装 `libgtk-3-dev libwebkit2gtk-4.1-dev xvfb`，则可完整复现 CI 命令（含 `-tags webview` 与 `xvfb-run`）；未安装时带 webview 标签的编译会失败，属预期。
5. `go list`/`go test` 为标准命令，可本地直接验证；完整 CI 等价命令待本地验证（取决于本机 GUI 依赖）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CI 用 `-coverpkg=$PACKAGES` 指定全部包，而不是让每个包只统计自己的覆盖率？
**答案**：`-coverpkg` 把指定包的插桩打进**被测进程**，跨包测试（如 `pkg/rest` 的测试穿过 api 层直达 download 层）也能给沿途代码记覆盖率，总量远大于各包自测之和，得到的 coverage.txt 才真实反映整条调用链的覆盖情况。

**练习 2**：Linux webview 作业为什么要 `go test -c` 手动编二进制、再 `-test.count=5` 重复执行？
**答案**：注释写明目标是「在同一 GTK 事件循环上重复，捕获引擎销毁后仍在排队的回调」。`-count=5` 让同一进程内五轮测试共享进程级状态（GTK 主循环、全局注册表），单次运行看似正常的销毁时序 bug 在重复后才会暴露；先 `-c` 是为了带上与主测试一致的 `-coverpkg` 插桩。

**练习 3**：如果只想在本地快速验证自己改动的 `pkg/download` 代码没破坏分组功能，最小命令是什么？
**答案**：`go test -run 'TestDownloader_TaskGroup|TestDownloader_Batch' ./pkg/download/`——正则同时匹配两组测试，跳过耗时的真实 HTTP 下载用例（那些用例生成 200MB 测试文件），几十秒内出结果。

## 5. 综合实践

**为 u3-l4 的自定义 `mock://` 协议补一组单元测试**，把本讲三层替身手法全部用一遍。在 `pkg/download/` 下新建 `mock_protocol_test.go`（示例代码，仅供练习）：

1. **Resolve 分支**：仿照 `generationTestFetcher`（[pkg/download/downloader_test.go:L128-L161](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L128-L161)），构造 `mock://ok` 与 `mock://bad` 两种 URL，断言前者 Resolve 成功返回单文件资源、后者返回错误且 Task 不产生。
2. **断点续传分支**：仿照 `recoveryTestManager`（[pkg/download/downloader_test.go:L191-L217](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L191-L217)）为协议实现 Store/Restore，写出「下载→暂停→Close→新 Downloader 重开同目录→Continue→完成」的恢复测试，参考 `TestDownloader_StoreAndRestore`（[pkg/download/downloader_test.go:L1195](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L1195)）的骨架。
3. **错误分支**：给 Fetcher 加一个 `done <- err` 的出错开关，断言任务进入 error 态且 `Wait()` 的错误被 watch 循环正确写回 `task.Error`。
4. **验证**：`go test -run 'TestMockProtocol' -v ./pkg/download/`，三条路径全绿即完成。若你的 u3-l4 实现与本仓库测试脚手架字段有出入，以让测试通过为准调整练习代码（不要改产品源码）。

## 6. 本讲小结

- **`internal/test` 假服务器家族**：一台服务器模拟一种线上故障（一次性 URL、忽略 Range、过期重定向、Cookie 失效、连接数限制、多给数据……），`rangeFileHandle` 是可注入违规行为的 Range 处理核心，200MB 随机数据 + 随机端口 + 自动清理保证隔离与确定性。
- **下载器级测试靠内存协议**：`generationTestManager`（holdOpen 卡运行态、通道卡竞态点、原子计数）+ 存储装饰器（按桶注入失败）+ 并发卡点装饰器（entered/release 通道），让最难的竞态与故障恢复变成确定性断言。
- **本轮新增约 1100 行批量/分组测试**直接为 u4-l4/u4-l6 的语义背书：组不是任务、首次落盘即带 ParentID、部分成功、rid 优先且失败不消费、删除失败回写 bucketTask 并恢复队列、并发清理不误删组元数据。
- **三层驱动共享同一套实现**：服务层 `Service.JSON` 进程内调用（无 HTTP）、REST 层真实往返（Mem/Bolt 双存储矩阵）、分类层纯单测表驱动——呼应 u8-l1「handler 不接触网络对象」的设计红利。
- **extmock 是探索工具不是测试**：不进 CI，供扩展开发者命令行驱动 Resolve/下载/eval JS，`go`/`rpc` 两种 WebView provider 分别适配本机与外部宿主。
- **CI 命令矩阵**：排除法选包 + `-tags webview` + `xvfb-run` + 跨包 `-coverpkg` 覆盖率 + codecov；webview 集成在三 OS 上用三种策略重复执行捕销毁竞态；四个构建检查作业在测试通过后串联。

## 7. 下一步学习建议

- 下一讲 **u11-l2 并发控制与优雅关闭**：本讲多次出现的 `waitForTaskStatus`、`runGeneration`、通道卡点，都将在锁清单与 Close 时序的框架下得到统一解释。
- 建议继续精读 [pkg/download/downloader_test.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go) 中的 `TestDownloader_StaleStartGenerationCannotRestartTask`（L577）与 `TestDownloader_ConcurrentResolvedCreation`（L4916），它们是竞态测试的进阶范本。
- 若对 webview 集成感兴趣，读 [internal/webview/integrationtest/contract.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/webview/integrationtest/contract.go) 看三种 provider 如何用同一份「合同测试」约束行为一致。
