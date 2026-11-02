# u5-l4 完成判定、校验和与 HTTP OptsExtra

## 1. 本讲目标

上一讲（u5-l3）我们看到了 HTTP Fetcher 如何在「坏服务器」面前保护文件完整性。本讲回答下载的最后一个问题：**字节都到齐之后，系统如何判定「真的完成了」，如何在收尾时校验文件内容，以及下载完成之后还会发生什么**。学完本讲，你应该能够：

1. 读懂 `checkCompletion` 与 `onDownloadComplete` 的完成判定规则（字节计数、连接完成度、403 容忍、完整性错误优先）。
2. 掌握 `ChecksumOption` 的算法归一化、哈希长度校验规则，以及 `verifyChecksum` 的流式校验流程。
3. 理解校验失败后的文件保留策略与「全量重下」机制（`checksumFailed` 标志）。
4. 说出 `OptsExtra` 每个字段在 Downloader 层的生效时机，以及任务级 `*bool` 三态设置如何覆盖全局配置。

## 2. 前置知识

### 2.1 为什么「完成判定」不是一个简单问题

回忆 u5-l2：一次多连接下载有若干个 `connection`（逻辑车道），每个连接负责一个 `chunk` 区间，慢启动控制器分批建连，干完活的连接还会「偷」最慢连接的后一半。于是「下载完成了吗」可能出现三种口径：

- **字节口径**：所有连接累计写入的字节数 ≥ 文件总大小。
- **连接口径**：所有连接都进入 `connCompleted`（或 `connFailed`）状态，没有活儿可干了。
- **完整性口径**：某些连接带着「终态完整性错误」（u5-l3 的哨兵错误）退出，此时哪怕字节数凑够了，文件也可能是坏的。

这三者可能互相矛盾（例如某连接 403 失败但别的连接把它那份下完了；或响应体格式错误但字节数恰好对上）。`onDownloadComplete` 必须给出一个统一的裁决。

### 2.2 校验和（checksum）基础

哈希函数可以把任意长度的字节流映射成固定长度的「指纹」。Gopeed 支持三种：

| 算法 | 指纹长度（字节） | 十六进制表示长度（字符） |
| --- | --- | --- |
| MD5 | 16 | 32 |
| SHA-1 | 20 | 40 |
| SHA-256 | 32 | 64 |

发布者公布文件的哈希值，下载者算一遍本地文件的哈希，两者一致即可确信内容没有损坏或被篡改。十六进制字符串大小写不敏感（`A3` 与 `a3` 等价），比对时需忽略大小写。

### 2.3 三态布尔：`*bool`

u2-l3 讲过「零值歧义」问题：Go 的 `bool` 零值是 `false`，无法区分「用户明确关掉」和「用户没说」。Gopeed 对任务级开关使用 `*bool`：

- `nil`：未设置，跟随全局配置；
- `&true` / `&false`：明确开启 / 关闭，覆盖全局配置。

### 2.4 事件流回顾（承接 u4-l3）

Fetcher 不直接改任务状态，只通过 `Wait()` 的返回值汇报结果：`nil` 表示成功，`error` 表示失败。Downloader 的 watch 协程阻塞在 `task.fetcher.Wait()` 上，把结果翻译成 `done`/`error` 状态迁移，并发射 `done`+`finally` 或 `error`+`finally` 事件。本讲的校验失败，正是以「`Wait()` 返回错误」的方式进入这条管道的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/protocol/http/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go) | HTTP Fetcher 主体：完成判定、校验执行、校验失败后的全量重下 |
| [pkg/protocol/http/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go) | 公开参数模型：`ChecksumOption` 校验选项与 `OptsExtra` 联动字段 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | watch 协程的完成分支：AutoTorrent / AutoExtract 等完成后联动 |
| [pkg/base/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go) | 全局配置 `AutoTorrentConfig` / `ArchiveConfig`（任务级未设置时的回退来源） |
| [internal/protocol/http/fetcher_checksum_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_checksum_test.go) | 校验功能的单元测试（本讲实践的重要依据） |
| [internal/protocol/http/fetcher_checksum_retry_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_checksum_retry_test.go) | 校验失败后重试全量重下的单元测试 |

## 4. 核心概念与源码讲解

### 4.1 checkCompletion 与 onDownloadComplete：完成判定与收尾流程

#### 4.1.1 概念说明

「完成判定」分两级：

- **`checkCompletion`（探测级）**：downloadLoop 在慢启动循环里周期性问「活儿是不是干完了？」——返回 `true` 只是说「不必再扩张连接了，去等收尾吧」，它**不做最终裁决**。
- **`onDownloadComplete`（裁决级）**：所有连接真正退出后（`wg.Wait()` 返回）执行，综合字节计数、连接状态、失败原因给出最终结论，然后关闭文件、执行校验、设置终态、向 `doneCh` 发送结果。

把探测和裁决分开，是为了避免「字节够了但连接还在写」时提前宣布完成。

#### 4.1.2 核心流程

```text
downloadLoop（慢启动循环）
  ├─ 每收到一个扩张信号 expansionCh：
  │    ├─ checkCompletion() == true → 不再扩张，转入等待
  │    └─ 否则 expandConnections() 继续建连
  └─ waitForCompletion(ctx)
       ├─ f.wg.Wait()               # 等所有 connection goroutine 退出
       └─ ctx 未取消 → onDownloadComplete()
            ├─ ① 汇总 totalDownloaded（resolveConn + 所有 connections）
            ├─ ② 检查 allChunksComplete（逐连接看还有没有剩余工作）
            ├─ ③ 字节口径：downloadComplete = 已知大小且 totalDownloaded >= Size
            ├─ ④ 完整性错误优先：任一连接带终态 Range 错误 → finalErr
            ├─ ⑤ 其余失败（容忍 403）→ finalErr
            ├─ ⑥ 关闭文件句柄（先关文件再校验）
            ├─ ⑦ finalErr == nil → verifyChecksum()
            │      ├─ 校验通过 → setState(stateDone)，doneCh <- nil
            │      └─ 校验失败 → setState(stateError)，doneCh <- 错误
            └─ ⑧ finalErr != nil → setState(stateError)，doneCh <- finalErr
```

#### 4.1.3 源码精读

downloadLoop 在慢启动循环的每个扩张信号里先探测完成度，完成则转入等待收尾，不再扩张连接——[internal/protocol/http/fetcher.go:964-999](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L964-L999)：

```go
// Slow start loop
for {
    select {
    case <-ctx.Done():
        // Paused or cancelled
        return
    case <-f.slowStart.expansionCh:
        ...
        // Batch completed, try to expand
        if f.checkCompletion() {
            // All work is done, wait for connections to finish
            f.waitForCompletion(ctx)
            return
        }
        f.expandConnections()
        ...
    }
}
```

`checkCompletion` 用两个判据回答「还有没有活儿」——[internal/protocol/http/fetcher.go:2332-2368](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2332-L2368)：

```go
if f.hasKnownSize() && f.meta.Res.Size > 0 && totalDownloaded >= f.meta.Res.Size {
    return true   // 判据一：字节口径，已写够总大小
}
allCompleted := true
if f.resolveConn != nil && !f.resolveConn.Completed && ... {
    allCompleted = false
}
for _, conn := range f.connections {
    if !conn.Completed && conn.State != connCompleted && conn.State != connFailed {
        allCompleted = false  // 判据二：还有连接既没完成也没失败（还会继续干活）
        break
    }
}
```

注意判据二把 `connFailed` 也视为「不再有活儿」——失败连接不会复活，探测级的职责只是决定「要不要继续扩张」，失败连接的账留给裁决级去算。

`waitForCompletion` 等待所有连接 goroutine 退出，并只在「本轮 Start 的 ctx 仍然存活」时触发收尾，防止暂停后旧协程误发完成信号——[internal/protocol/http/fetcher.go:2180-2186](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2180-L2186)：

```go
func (f *Fetcher) waitForCompletion(ctx context.Context) {
	f.wg.Wait()
	// Only trigger completion for the Start cycle that launched this waiter.
	if ctx != nil && ctx.Err() == nil {
		f.onDownloadComplete()
	}
}
```

`onDownloadComplete` 是裁决中心。先看 403 容忍逻辑——某连接因服务器连接数上限被 403 拒绝，但只要文件大小已知且别的连接能补上它的份，就不算失败——[internal/protocol/http/fetcher.go:2201-2228](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2201-L2228)：

```go
if conn.State == connFailed && conn.failed {
    if re := extractRequestError(conn.lastErr); re != nil && re.Code == 403 && f.hasKnownSize() {
        // 403 is server connection limit, check if other connections will complete this chunk
        continue
    }
}
...
// If total downloaded matches file size, consider it a success regardless of connection failures
downloadComplete := f.hasKnownSize() && f.meta.Res.Size > 0 && totalDownloaded >= f.meta.Res.Size
```

接着是关键的优先级规则：**完整性错误压过字节计数**。一个格式错误的响应可能在「预期字节数已写满」之后才被发现，此时文件内容是坏的，不能因为字节数对上就宣布成功——[internal/protocol/http/fetcher.go:2230-2258](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2230-L2258)：

```go
// Integrity errors must win over byte-count completion. A malformed response
// may be detected only after the expected bytes have already been written.
var finalErr error
for _, conn := range f.connections {
    if conn.State == connFailed && conn.failed && isTerminalRangeError(conn.lastErr) {
        finalErr = fmt.Errorf("connection %d failed: retries=%d, err=%w", conn.ID, conn.retryTimes, conn.lastErr)
        break
    }
}
// Check for other errors, but ignore 403 ... if download completed.
if finalErr == nil && !downloadComplete && !allChunksComplete { ... }
```

裁决之后、校验之前，必须先关闭文件句柄（保证校验读到的数据全部落盘可见，也释放描述符），然后按「先校验、后定态、再通知」的顺序收尾——[internal/protocol/http/fetcher.go:2261-2285](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2261-L2285)：

```go
f.fileMu.Lock()
if f.file != nil {
    f.file.Close()
    f.file = nil
}
f.fileMu.Unlock()

if finalErr == nil {
    if chkErr := f.verifyChecksum(); chkErr != nil {
        finalErr = chkErr
        f.setState(stateError)   // 校验失败同样落入 error 终态
    } else {
        f.setState(stateDone)
    }
} else {
    f.setState(stateError)
}

select {
case f.doneCh <- finalErr:      // 容量 1 的通道，非阻塞发送
default:
}
```

除了这条主路，还有一条「捷径」：如果 Resolve 阶段的预取数据（u5-l1）已经覆盖了整个文件，`expandConnections` 建首连接时直接发现无需再连，就地走同一套「关文件 → 校验 → 定态 → 通知」——[internal/protocol/http/fetcher.go:1049-1075](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1049-L1075)：

```go
if totalSize > 0 && prefetched >= totalSize {
    f.connMu.Unlock()
    // Close the file before signaling completion
    ...
    var err error
    if chkErr := f.verifyChecksum(); chkErr != nil {
        err = chkErr
        f.setState(stateError)
    } else {
        f.setState(stateDone)
    }
    f.doneCh <- err
    return
}
```

Downloader 侧的 watch 协程阻塞在 `Wait()` 上接收这个结果——[internal/protocol/http/fetcher.go:2606-2608](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2606-L2608)：

```go
func (f *Fetcher) Wait() error {
	return <-f.doneCh
}
```

#### 4.1.4 代码实践

**实践目标**：通过现成测试观察完成判定的两条出口（成功与校验失败）。

1. 操作步骤：在仓库根目录运行：

```bash
go test ./internal/protocol/http/ -run 'TestFetcher_ChecksumMismatch_Retry' -v
```

2. 需要观察的现象：测试日志中第一次 `f.Wait()` 返回 `checksum mismatch` 错误且 fetcher 状态为 `stateError`；第二次 `f.Start()` 后 `Wait()` 返回 `nil`。
3. 预期结果：测试通过。对照 [fetcher_checksum_retry_test.go:97-121](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_checksum_retry_test.go#L97-L121) 的断言顺序，确认错误正是从 `doneCh` 经 `Wait()` 传出的。
4. 本实践为运行项目已有测试，命令可在本地直接执行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `checkCompletion` 把 `connFailed` 的连接也算作「没有剩余工作」？这样不会把失败任务误判为完成吗？

**答案**：`checkCompletion` 只服务于 downloadLoop 的「是否继续扩张连接」决策。失败连接不会自己复活，也不需要新连接去「替它工作」（扩张逻辑本来就只切分未完成连接的区间）。最终成败由 `onDownloadComplete` 裁决——那里会逐连接检查 `connFailed && conn.failed` 并生成 `finalErr`，所以不会误判为成功。

**练习 2**：字节计数已经凑够 `Res.Size`，但某连接带着 `isTerminalRangeError` 错误退出，任务最终是什么状态？为什么？

**答案**：`error` 状态。因为「完整性错误必须压过字节计数完成」——格式错误的响应可能在预期字节数写满之后才被发现，若按字节口径宣布成功就会产出损坏文件。这与 u5-l3「完整性优先于字节凑齐」的原则一脉相承。

### 4.2 verifyChecksum：下载完成后的哈希校验

#### 4.2.1 概念说明

`verifyChecksum` 是**可选（opt-in）**的内容校验：只有用户在 `OptsExtra.Checksum` 里显式提供了期望哈希，它才会工作；没配置就直接返回 `nil`，行为与旧版本完全兼容。它在文件关闭后运行，用 64KB 缓冲流式读取整个文件计算哈希，避免大文件一次性载入内存。校验失败会设置 `checksumFailed` 原子标志——这个标志将在下次重试时触发「全量重下」（见 4.3）。

#### 4.2.2 核心流程

```text
verifyChecksum()
  ├─ 守卫：meta/Opts/Extra 为 nil，或 Extra 不是 *OptsExtra，或 Checksum 为 nil → 跳过
  ├─ 守卫：Algorithm 与 Expected 同时为空 → 跳过
  ├─ 算法归一化：小写化 + 去空白
  │    md5 → md5.New()；sha1/sha-1 → sha1.New()；sha256/sha-256 → sha256.New()
  │    其它 → 返回 "unsupported checksum algorithm"（防御性，正常已被 Validate 拦截）
  ├─ 打开 f.meta.SingleFilepath()（此刻文件已被关闭，可安全读取）
  ├─ io.CopyBuffer(h, file, 64KB)  流式计算
  ├─ actual = hex(h.Sum(nil))
  ├─ expected = 小写化(去空白(用户输入))
  └─ strings.EqualFold(actual, expected)？
       ├─ 相等 → return nil
       └─ 不等 → checksumFailed.Store(true)；
                  return "checksum mismatch: expected X, got Y"
```

#### 4.2.3 源码精读

算法选择与别名归一化在这里完成——`sha-1`、`sha-256` 这类带连字符的别名与短名等价，统一小写比较——[internal/protocol/http/fetcher.go:2299-2309](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2299-L2309)：

```go
var h hash.Hash
switch strings.ToLower(strings.TrimSpace(chk.Algorithm)) {
case "md5":
    h = md5.New()
case "sha1", "sha-1":
    h = sha1.New()
case "sha256", "sha-256":
    h = sha256.New()
default:
    return fmt.Errorf("unsupported checksum algorithm: %s", chk.Algorithm)
}
```

校验目标是 `f.meta.SingleFilepath()`（u3-l1 讲过的路径计算方法），用 64KB 缓冲流式读取——[internal/protocol/http/fetcher.go:2311-2321](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2311-L2321)：

```go
filePath := f.meta.SingleFilepath()
file, err := os.Open(filePath)
if err != nil {
    return fmt.Errorf("open file for checksum verification failed: %w", err)
}
defer file.Close()

buf := make([]byte, 64*1024)
if _, err := io.CopyBuffer(h, file, buf); err != nil {
    return fmt.Errorf("calculate checksum failed: %w", err)
}
```

比对用 `strings.EqualFold`（忽略大小写的十六进制比较），失败时置位 `checksumFailed` 并返回带期望值/实际值的错误——[internal/protocol/http/fetcher.go:2323-2329](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2323-L2329)：

```go
actual := hex.EncodeToString(h.Sum(nil))
expected := strings.ToLower(strings.TrimSpace(chk.Expected))
if !strings.EqualFold(actual, expected) {
    f.checksumFailed.Store(true)
    return fmt.Errorf("checksum mismatch: expected %s, got %s", expected, actual)
}
return nil
```

对应测试验证了大小写不敏感比对——期望哈希全大写也能通过：[fetcher_checksum_test.go:162-189](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_checksum_test.go#L162-L189)。

#### 4.2.4 代码实践

**实践目标**：亲手复现「校验通过」与「校验失败」两种结果，并观察失败时文件的保留情况。

1. 实践准备：任选一个目录 `demo/` 放一个测试文件并起本地静态服务器：

```bash
mkdir -p /tmp/gopeed-demo && cd /tmp/gopeed-demo
echo "hello gopeed checksum" > demo.txt
md5sum demo.txt        # 记下正确的 md5，例如 6f3fxxxx...（32 个字符）
python3 -m http.server 18080
```

2. 操作步骤：创建 `_examples/checksumdemo/main.go`（**示例代码**，仿照 [_examples/basic/main.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/_examples/basic/main.go) 编写）：

```go
package main

import (
	"fmt"

	"github.com/GopeedLab/gopeed/pkg/base"
	"github.com/GopeedLab/gopeed/pkg/download"
	"github.com/GopeedLab/gopeed/pkg/protocol/http"
)

func main() {
	finallyCh := make(chan error)
	_, err := download.Boot().
		URL("http://127.0.0.1:18080/demo.txt").
		Listener(func(event *download.Event) {
			if event.Key == download.EventKeyFinally {
				finallyCh <- event.Err
			}
		}).
		Create(&base.Options{
			Path: "/tmp/gopeed-demo/download",
			Extra: http.OptsExtra{
				Connections: 1,
				Checksum: &http.ChecksumOption{
					Algorithm: "md5",
					// 第一次填 md5sum 的正确输出；第二次故意改成错误值
					Expected: "0123456789abcdef0123456789abcdef",
				},
			},
		})
	if err != nil {
		panic(err)
	}
	fmt.Printf("finally: %v\n", <-finallyCh)
}
```

运行：`go run ./_examples/checksumdemo/main.go`。

3. 需要观察的现象：正确哈希时打印 `finally: <nil>`，下载目录出现 `demo.txt`；错误哈希时打印 `finally: checksum mismatch: expected 0123..., got <真实md5>`。
4. 预期结果：**错误哈希场景下，下载目录中的 `demo.txt` 仍然存在且内容完整（大小正确）**——Fetcher 不会删除校验失败的文件，任务进入 `error` 状态，把「保留还是删除」的决定留给用户。可用 `md5sum /tmp/gopeed-demo/download/demo.txt` 验证其内容与源文件一致。
5. 程序行为已对照 [fetcher_checksum_test.go:97-128](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_checksum_test.go#L97-L128) 的断言推理，具体输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `verifyChecksum` 必须在 `f.file.Close()` 之后执行？

**答案**：一是数据可见性——校验要重新以只读方式打开磁盘文件读取全部内容，先关闭写句柄保证缓冲数据落盘、读到的是最终内容；二是资源管理——`onDownloadComplete` 之后 `Wait()` 即将返回，文件句柄必须在通知前释放，否则任务暂停/删除时可能泄漏描述符。

**练习 2**：128KB 的大文件校验为什么不会占用 128KB 以上内存？

**答案**：实现用 `io.CopyBuffer(h, file, make([]byte, 64*1024))` 流式读取，每次最多 64KB 装入缓冲，哈希对象 `h` 内部增量更新，内存占用恒定。[fetcher_checksum_test.go:220-263](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_checksum_test.go#L220-L263) 专门测试了超过 64KB 的文件。

### 4.3 ChecksumOption：校验选项的归一化与全量重下

#### 4.3.1 概念说明

`ChecksumOption` 是公开模型（`pkg/protocol/http/model.go`）上的校验选项。它的设计要点有二：

1. **提前校验（fail fast）**：选项合法性在 `Start()` 一开始的 `initOptions` 里就通过 `Validate()` 检查，非法配置直接让 `Start` 返回错误——**一个字节都不会请求**。这比「下载完才发现算法名拼错」友好得多。
2. **失败可自愈**：校验失败置位 `checksumFailed` 标志；用户重试（再次 `Start`）时，该标志触发「截断文件 + 重置全部连接状态」的全量重下，防止「连接显示 Completed 但字节是坏的」被断点续传逻辑跳过。

#### 4.3.2 核心流程

```text
Start()
  └─ initOptions()
       └─ extra.Checksum.Validate()
            ├─ IsEmpty（Algorithm 与 Expected 皆空）→ nil，跳过校验
            ├─ 算法归一化失败 → "unsupported checksum algorithm: X"
            ├─ Expected 为空 → "checksum expected hash is required..."
            ├─ 长度不符 → md5=32 / sha1=40 / sha256=64 字符
            └─ 非十六进制 → "expected valid hexadecimal string"

校验失败后（checksumFailed = true）用户重试：
Start()（stateError 分支）→ doStart()
  └─ fullRedownload := checksumFailed.Swap(false)   # 取走并清除标志
       ├─ true → os.Truncate(file, 0)               # 清空坏文件
       │         prepareFullRedownload()             # 重置连接表/预取状态
       └─ 重置所有连接（fullRedownload 时连 Completed 的也重置）
```

#### 4.3.3 源码精读

`ChecksumOption` 结构与两个辅助方法：`IsEmpty` 判空、`NormalizedAlgorithm` 把 `sha-1`/`sha-256` 别名归一化为规范短名——[pkg/protocol/http/model.go:15-47](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L15-L47)：

```go
type ChecksumOption struct {
	Algorithm string `json:"algorithm"` // "md5" | "sha1" | "sha256"
	Expected  string `json:"expected"`  // hex-encoded expected hash
}

func (chk *ChecksumOption) NormalizedAlgorithm() (string, bool) {
	...
	switch strings.ToLower(strings.TrimSpace(chk.Algorithm)) {
	case "md5":
		return "md5", true
	case "sha1", "sha-1":
		return "sha1", true
	case "sha256", "sha-256":
		return "sha256", true
	default:
		return "", false
	}
}
```

`Validate` 的完整规则链：空选项放行 → 算法受支持 → 哈希必填 → **长度精确匹配算法指纹的十六进制字符数** → 必须是合法十六进制——[pkg/protocol/http/model.go:49-79](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L49-L79)：

```go
var expectedLen int
switch algo {
case "md5":
    expectedLen = 32
case "sha1":
    expectedLen = 40
case "sha256":
    expectedLen = 64
}
if len(expected) != expectedLen {
    return fmt.Errorf("invalid checksum hash length for %s: expected %d characters, got %d", algo, expectedLen, len(expected))
}
if _, err := hex.DecodeString(expected); err != nil {
    return fmt.Errorf("invalid checksum hash for %s: expected valid hexadecimal string", algo)
}
```

`Validate` 的唯一调用点是 `Start` → `initOptions`，与连接数回退一起构成「启动前参数整备」——[internal/protocol/http/fetcher.go:743-767](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L743-L767)：

```go
func (f *Fetcher) initOptions() error {
	...
	if extra.Connections <= 0 {
		extra.Connections = f.config.Connections
		...
	}
	if err := extra.Checksum.Validate(); err != nil {
		return err
	}
	return nil
}
```

测试证明「非法选项不产生任何额外网络请求」——Resolve 后计数、Start 失败后再计数，两次相等——[fetcher_checksum_test.go:290-326](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_checksum_test.go#L290-L326)。同时注意：算法不合法属于参数错误，**不会**置位 `checksumFailed`（[fetcher_checksum_test.go:157-159](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_checksum_test.go#L157-L159)），因为压根没下载过任何字节。

`checksumFailed` 字段的注释说清了它的使命——[internal/protocol/http/fetcher.go:293-297](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L293-L297)：

```go
// checksumFailed marks that the last completed download failed
// checksum verification, so the next retry must force a full
// re-download instead of skipping connections that already
// reported Completed.
checksumFailed atomic.Bool
```

重试路径：`doStart` 发现上次是 `stateError` 时，用 `Swap(false)` 原子地取走标志，为真则**先截断文件再重置状态**——截断是为了防止「重试被打断时，坏字节残留在文件里被误当作已下载进度」——[internal/protocol/http/fetcher.go:817-848](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L817-L848)：

```go
if state == stateError {
    fullRedownload := f.checksumFailed.Swap(false)
    if fullRedownload {
        // Checksum mismatch: the file content itself is wrong even
        // though every connection reported Completed. Truncate the
        // corrupt file so stale bytes cannot linger if the retry is
        // interrupted before every connection re-writes its chunk.
        if err := os.Truncate(f.meta.SingleFilepath(), 0); err != nil && !os.IsNotExist(err) {
            return fmt.Errorf("failed to reset file after checksum mismatch: %w", err)
        }
        f.prepareFullRedownload()
    }
    f.connMu.Lock()
    for _, conn := range f.connections {
        // On a checksum mismatch, reset EVERY connection —
        // "Completed" only means the download loop finished, not
        // that the bytes were verified correct.
        shouldReset := fullRedownload || (!conn.Completed && conn.State != connCompleted)
        ...
    }
    f.connMu.Unlock()
}
```

`prepareFullRedownload` 把连接表、Range 探测资格、校验器钉扎、预取状态全部归零，让下次 Start 走「全新开始」路径——[internal/protocol/http/fetcher.go:726-740](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L726-L740)。

#### 4.3.4 代码实践

**实践目标**：用表驱动方式穷举 `Validate` 的全部拒绝分支，加深对规则链的记忆。

1. 操作步骤（**示例代码**，可放入 `_examples/checksumdemo/validate_test.go` 或任意独立测试文件）：

```go
package main

import (
	"fmt"

	"github.com/GopeedLab/gopeed/pkg/protocol/http"
)

func main() {
	cases := []http.ChecksumOption{
		{Algorithm: "sha3", Expected: "abc"},                       // 不支持的算法
		{Algorithm: "md5", Expected: ""},                           // 算法有、哈希缺失
		{Algorithm: "md5", Expected: "tooshort"},                   // 长度不是 32
		{Algorithm: "sha1", Expected: "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"}, // 长度对但非十六进制
		{Algorithm: "SHA-256", Expected: "64位合法hex"},            // 别名 + 大小写（应通过）
		{},                                                        // 全空（应通过=跳过）
	}
	for i, c := range cases {
		fmt.Printf("case %d: err=%v\n", i, c.Validate())
	}
}
```

2. 需要观察的现象：前四个 case 分别输出算法不支持、哈希必填、长度不符、非法十六进制四类错误；最后两个输出 `<nil>`。
3. 预期结果：与 [pkg/protocol/http/model.go:49-79](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L49-L79) 的错误信息逐字对应（其中 case 5 需把「64位合法hex」换成真实的 64 个十六进制字符）。
4. 具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么校验失败后的重试要**重置所有**连接，包括那些 `Completed == true` 的？

**答案**：连接的 `Completed` 只表示「下载循环正常跑完」，不表示「写出的字节正确」。校验失败恰恰证明至少某些「已完成」连接写的是坏字节。如果按普通错误重试（只重置未完成的连接），坏字节会被断点续传逻辑跳过，重试后文件依然是坏的。

**练习 2**：`checksumFailed.Swap(false)` 为什么用 `Swap` 而不是 `Load`？

**答案**：`Swap` 在读取的同时清除标志，保证「一次失败只触发一次全量重下」。若用 `Load`，标志会一直残留，之后任何普通错误的重试都会被误判为需要全量重下，浪费已正确下载的字节。

**练习 3**：算法名写成 `sha-256`（带连字符）、期望哈希写成全大写，这次下载会失败吗？

**答案**：不会。`NormalizedAlgorithm` 接受 `sha-256` 别名，`verifyChecksum` 的算法 switch 同样接受；哈希比对用 `strings.EqualFold` 忽略大小写。两个容错点分别位于 [internal/protocol/http/fetcher.go:2300-2306](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2300-L2306) 与 [internal/protocol/http/fetcher.go:2324-2325](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2324-L2325)。

### 4.4 OptsExtra 联动字段：下载完成之后发生什么

#### 4.4.1 概念说明

`OptsExtra`（[pkg/protocol/http/model.go:81-98](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L81-L98)）的七个字段按生效时机分三类：

| 类别 | 字段 | 生效时机 | 生效位置 |
| --- | --- | --- | --- |
| 传输参数 | `Connections` | `Start()` 的 `initOptions` / `doStart` | HTTP Fetcher（u5-l2 慢启动的连接上限） |
| 完成校验 | `Checksum` | `Start()` 校验配置；下载收尾时执行 | HTTP Fetcher（本讲 4.2/4.3） |
| 完成后联动 | `AutoTorrent`、`DeleteTorrentAfterDownload`、`AutoExtract`、`ArchivePassword`、`DeleteAfterExtract` | 任务 `done` 之后 | **Downloader 的 watch 协程** |

关键认知：后五个字段 HTTP Fetcher 自己**完全不看**，它们由 `pkg/download/downloader.go` 在任务完成后消费。这正是「Fetcher 管传输、Downloader 管生命周期」分工的又一次体现。

三态覆盖规则（`*bool` 字段统一适用）：

```text
任务级 e.AutoTorrent != nil ？── 是 → 用 *e.AutoTorrent（任务说了算）
                              └─ 否 → 用全局 cfg.AutoTorrent.Enable
```

#### 4.4.2 核心流程

watch 协程成功分支的完整顺序（承接 u4-l3）：

```text
fetcher.Wait() 返回 nil
  ├─ markTaskDone(task)                      # 一次性裁决，防旧协程覆盖
  ├─ captureTaskStats + storage.Put(bucketTask, ...)   # 最终快照落盘
  ├─ emit(done) → emit(finally)
  ├─ notifyRunning()                         # 唤醒等待队列中的下一个任务
  ├─ releaseBlobTask / triggerOnDone         # 扩展钩子（u9-l3）
  ├─ triggerWebhooks / triggerScripts        # 外发通知（u10-l3）
  └─ postDownloadExtra(task.Meta) != nil ？
       ├─ AutoTorrent 链：启用 && 文件以 .torrent 结尾
       │    └─ go CreateDirect(torrent 文件路径, 同目录)   # 异步建 BT 任务
       │         └─ shouldDelete → Delete(原任务, force=true)  # 连文件删
       └─ AutoExtract 链：启用 && isArchiveFile(路径)
            └─ enqueueExtraction(task, 路径, extra)        # 进全局串行解压队列
                 └─ ArchivePassword / DeleteAfterExtract 随任务传入
```

#### 4.4.3 源码精读

`OptsExtra` 的完整定义与每个字段的三态语义注释——[pkg/protocol/http/model.go:81-98](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L81-L98)：

```go
type OptsExtra struct {
	Connections int `json:"connections"`
	// AutoTorrent when task download complete, and it is a .torrent file,
	// it will be auto create a new task for the torrent file
	// nil means use global config, true/false means explicit setting
	AutoTorrent *bool `json:"autoTorrent"`
	// DeleteTorrentAfterDownload when true, deletes the .torrent file after creating BT task
	DeleteTorrentAfterDownload *bool `json:"deleteTorrentAfterDownload"`
	// AutoExtract when task download complete, and it is an archive file, it will be auto extracted
	AutoExtract *bool `json:"autoExtract"`
	// ArchivePassword is the password for extracting password-protected archives
	ArchivePassword string `json:"archivePassword"`
	// DeleteAfterExtract when true, deletes the archive file after successful extraction
	DeleteAfterExtract bool `json:"deleteAfterExtract"`
	// Checksum verifies the completed file against an expected hash after download
	Checksum *ChecksumOption `json:"checksum,omitempty"`
}
```

Downloader 成功分支：先做状态迁移与事件外发，最后才处理联动字段——[pkg/download/downloader.go:1583-1601](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1583-L1601)：

```go
if !d.markTaskDone(task) {
    return
}
...
d.emit(EventKeyDone, task)
d.emit(EventKeyFinally, task, err)
d.notifyRunning()
d.releaseBlobTask(task)
d.triggerOnDone(task)
d.triggerWebhooks(WebhookEventDownloadDone, task, nil)
d.triggerScripts(ScriptEventDownloadDone, task, nil)

if e := postDownloadExtra(task.Meta); e != nil {
```

AutoTorrent 的三态回退与触发条件——任务级优先、全局兜底，且要求文件以 `.torrent` 结尾——[pkg/download/downloader.go:1606-1641](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1606-L1641)：

```go
autoTorrentEnabled := false
if e.AutoTorrent != nil {
    autoTorrentEnabled = *e.AutoTorrent
} else if cfg != nil && cfg.AutoTorrent != nil {
    autoTorrentEnabled = cfg.AutoTorrent.Enable
}

if autoTorrentEnabled && strings.HasSuffix(downloadFilePath, ".torrent") {
    shouldDelete := false
    if e.DeleteTorrentAfterDownload != nil {
        shouldDelete = *e.DeleteTorrentAfterDownload
    } else if cfg != nil && cfg.AutoTorrent != nil {
        shouldDelete = cfg.AutoTorrent.DeleteAfterDownload
    }
    go func() {
        _, err2 := d.CreateDirect(&base.Request{URL: downloadFilePath}, &base.Options{
            Path:        task.Meta.Opts.Path,   // BT 任务沿用同一下载目录
            SelectFiles: make([]int, 0),
        })
        ...
        if shouldDelete {
            d.Delete(&TaskFilter{IDs: []string{task.ID}}, true)  // force=true 连文件删
        }
    }()
}
```

注意这里的 `CreateDirect` 传入的是**本地 .torrent 文件路径**——u3-l2 讲过，file 型 `SchemeFilter` 靠扩展名匹配，`new(FetcherManager)`（BT）会接手这个路径并链式创建 BT 任务。

AutoExtract 同样三态回退，命中后进入全局串行解压队列——[pkg/download/downloader.go:1643-1656](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1643-L1656)：

```go
autoExtractEnabled := false
if e.AutoExtract != nil {
    autoExtractEnabled = *e.AutoExtract
} else if cfg != nil && cfg.Archive != nil {
    autoExtractEnabled = cfg.Archive.AutoExtract
}
// Auto-extract archive files using the extraction queue
// This ensures only one extraction runs at a time to prevent resource exhaustion
if autoExtractEnabled && isArchiveFile(downloadFilePath) {
    d.enqueueExtraction(task, downloadFilePath, e)
}
```

`postDownloadExtra` 是协议适配器：HTTP 的 `OptsExtra` 直接用；FTP 单文件任务把自己的同名字段映射成 HTTP 形状，共享同一套联动逻辑——[pkg/download/downloader.go:1661-1686](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1661-L1686)：

```go
func postDownloadExtra(meta *fetcher.FetcherMeta) *http.OptsExtra {
	...
	switch e := meta.Opts.Extra.(type) {
	case *http.OptsExtra:
		return e
	case *ftp.OptsExtra:
		if meta.Res == nil || meta.Res.Name != "" || len(meta.Res.Files) != 1 {
			return nil
		}
		return &http.OptsExtra{
			Connections:                e.Connections,
			AutoTorrent:                e.AutoTorrent,
			...
		}
	}
	return nil
}
```

全局配置侧的回退来源——[pkg/base/model.go:419-429](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L419-L429)：

```go
// AutoTorrentConfig is the auto torrent task creation configuration
type AutoTorrentConfig struct {
	Enable              bool `json:"enable"`
	DeleteAfterDownload bool `json:"deleteAfterDownload"`
}

// ArchiveConfig is the archive extraction configuration
type ArchiveConfig struct {
	AutoExtract        bool `json:"autoExtract"`
	DeleteAfterExtract bool `json:"deleteAfterExtract"`
}
```

还有一个易被忽略的细节：`ArchivePassword` 与 `DeleteAfterExtract` **没有**全局回退（前者是 `string`，后者是 `bool` 而非 `*bool`），只能随任务提供；它们被解压队列原样消费——[pkg/download/downloader.go:2477-2482](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2477-L2482)：

```go
extractErr := extractArchive(archivePath, destDir, opts.ArchivePassword, func(...) { ... })
d.handleExtractionResult(task, extractErr, []string{archivePath}, opts.DeleteAfterExtract)
```

#### 4.4.4 代码实践

**实践目标**：整理 `OptsExtra` 字段生效时机表，并用任务级设置覆盖全局配置做一次 AutoTorrent 实验。

1. 操作步骤：
   - 先填写下面的表格（答案见「预期结果」）：

| 字段 | 生效时机 | 消费方 | 任务级覆盖全局？ |
| --- | --- | --- | --- |

   - AutoTorrent 实验：找一个 `.torrent` 文件放到本地静态服务器上，用 4.2.4 的示例程序把 `Checksum` 字段去掉、改设 `AutoTorrent: util.BoolPtr(true)`（`util.BoolPtr` 见 [pkg/util](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util)），或直接写 `yes := true; AutoTorrent: &yes`。

2. 需要观察的现象：HTTP 任务完成后，任务列表里**自动多出一个 BT 任务**（URL 是 .torrent 文件的本地路径），两个任务同处一个下载目录；若同时设 `DeleteTorrentAfterDownload: &yes`，原 HTTP 任务连同 .torrent 文件一起消失。
3. 预期结果：表格答案——`Connections` 在 `Start()` 时由 HTTP Fetcher 消费（不覆盖全局，而是「任务级 > 协议全局配置 > 1」三级回退，见 u1-l4）；`Checksum` 在 `Start()` 校验配置、收尾时由 HTTP Fetcher 执行（无全局对应项）；`AutoTorrent`/`DeleteTorrentAfterDownload`/`AutoExtract` 在任务 done 后由 Downloader 消费，`*bool` 三态覆盖全局；`ArchivePassword`/`DeleteAfterExtract` 在解压时由 Downloader 消费，无全局回退。可对照 [pkg/download/downloader_test.go:3671-3932](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L3671) 的 `TestDownloader_AutoTorrent` 系列测试验证行为。
4. 网络与种子可用性依赖本地环境，具体现象待本地验证；测试 `go test ./pkg/download/ -run TestDownloader_AutoTorrent -v` 可稳定复现。

#### 4.4.5 小练习与答案

**练习 1**：为什么 AutoTorrent / AutoExtract 的消费代码放在 Downloader 而不是 HTTP Fetcher 的 `onDownloadComplete` 里？

**答案**：其一，这两个功能与 HTTP 协议无关——`postDownloadExtra` 同时适配 FTP 单文件任务，放在协议层会重复实现；其二，链式动作（创建 BT 任务、入解压队列）需要 Downloader 级能力（`CreateDirect`、任务队列、存储），Fetcher 无权访问；其三，Fetcher 的职责边界是「把文件下对」，生命周期联动属于 Downloader 的调度职责（u3-l1 的契约）。

**练习 2**：一个 FTP **目录**任务（多文件 Resource）下载完成后会触发 AutoExtract 吗？

**答案**：不会。`postDownloadExtra` 对 FTP 分支显式要求 `meta.Res.Name == "" && len(meta.Res.Files) == 1`（单文件），目录任务返回 `nil`，整段联动逻辑直接跳过——目录没有「单个归档文件」可供解压。

**练习 3**：全局配置 `autoTorrent.enable = true`，但某个任务显式设置 `autoTorrent = false`，下载 .torrent 文件后会创建 BT 任务吗？

**答案**：不会。任务级 `e.AutoTorrent != nil` 分支优先，`*e.AutoTorrent` 为 `false`，覆盖全局开关。这正是 `*bool` 三态设计存在的意义。[pkg/download/downloader.go:1607-1612](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1607-L1612)。

## 5. 综合实践

**任务：写一个「带校验与自动种子」的迷你下载器，完整走一遍本讲链路。**

在 `_examples/checksumdemo/` 目录扩展 4.2.4 的示例程序，实现三个子命令式行为：

1. `go run ./_examples/checksumdemo/main.go -check`：下载 `demo.txt` 并用**错误**的 md5 校验。
   - 预期：`finally` 事件携带 `checksum mismatch` 错误；任务状态为 `error`；**文件仍保留**在下载目录（用 `md5sum` 验证内容其实完好）。
2. 接着实现重试：程序中在收到 `finally` 错误后，调用 `defaultDownloader.Continue(&download.TaskFilter{IDs: []string{taskID}})`（先保存 `Create` 返回的任务 ID；`Continue` 的语义见 u4-l4），并把服务器端文件换成正确内容或修正 `Expected`。
   - 预期：观察日志中的重试走「截断文件 + 全量重下」路径（对照 [internal/protocol/http/fetcher.go:817-848](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L817-L848)）；第二次 `finally` 为 `<nil>`。
3. `-torrent`：下载一个 `.torrent` 文件，任务级设置 `AutoTorrent: &yes`，观察自动链式创建的 BT 任务。

完成后，把观察记录成一张「事件 × 文件状态」表：

| 场景 | finally 事件 Err | 任务状态 | 磁盘文件 |
| --- | --- | --- | --- |
| 正确 md5 | nil | done | 完整保留 |
| 错误 md5 | checksum mismatch | error | 完整保留（内容其实是好的） |
| 错误 md5 后重试 | nil | done | 被截断后重写 |

第 2 步涉及修改任务参数后重试的具体 API 组合，行为推理依据 [fetcher_checksum_retry_test.go:59-130](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_checksum_retry_test.go#L59-L130)，完整运行效果待本地验证。

## 6. 本讲小结

- 完成判定分两级：`checkCompletion` 只回答「要不要继续扩张连接」，`onDownloadComplete` 才做最终裁决；裁决规则是**完整性错误 > 字节计数 > 连接完成度**，且 403（服务器连接上限）在文件大小已知时被容忍。
- 收尾顺序固定：关文件 → `verifyChecksum` → 设置 `stateDone`/`stateError` → 向 `doneCh` 发送结果；Downloader 的 watch 协程经 `Wait()` 接收并翻译成任务状态与事件。
- `ChecksumOption` 是 opt-in 校验：`Validate` 在 `Start()` 最提前失败（非法配置零网络请求），规则链为「空则跳过 → 算法归一化（含 sha-1/sha-256 别名）→ 哈希必填 → 长度 32/40/64 → 合法十六进制」。
- 校验比对忽略十六进制大小写；失败不删除文件——错误内容原样保留，去留交给用户；重试时 `checksumFailed` 标志触发「截断 + 全量重下」，因为连接的 `Completed` 不代表字节正确。
- `OptsExtra` 七字段分三类生效：`Connections` 由 Fetcher 在启动时消费；`Checksum` 由 Fetcher 在启动与收尾时消费；其余五个（AutoTorrent/DeleteTorrentAfterDownload/AutoExtract/ArchivePassword/DeleteAfterExtract）由 **Downloader 在任务 done 后**消费，`*bool` 字段遵循「任务级显式设置覆盖全局配置」的三态规则，且经 `postDownloadExtra` 同时服务 HTTP 与 FTP 单文件任务。

## 7. 下一步学习建议

本讲结束了 HTTP 协议深潜（u5 单元）。接下来：

- **u6-l1 BT 客户端、种子与磁力解析**：本讲 AutoTorrent 链式创建的 BT 任务将在这里展开——`CreateDirect` 收到 .torrent 文件路径后如何被 BT FetcherManager 接管。
- **u10-l2 归档自动解压与多卷处理**：本讲只追到 `enqueueExtraction` 入队，串行队列、多卷收集、状态回写的完整机制在该讲深潜。
- **u10-l3 Webhook 与脚本钩子**：本讲看到的 `triggerWebhooks`/`triggerScripts` 是下载完成事件外发的另外两条通道，与校验/联动字段互不影响。
- 若想巩固「完整性优先」主题，可回头对照 u5-l3 的三个哨兵错误，理解 `isTerminalRangeError` 为何能在 `onDownloadComplete` 中一票否决字节计数。
