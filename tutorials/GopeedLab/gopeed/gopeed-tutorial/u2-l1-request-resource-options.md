# Request、Resource、Options：一次下载的三要素

## 1. 本讲目标

学完本讲，你应该能够：

- 逐字段解释 `Request`、`Resource`、`FileInfo`、`Options` 四个结构体的含义与相互关系。
- 理解 `Extra` 字段为什么声明为 `any`，以及 `ParseReqExtra` / `ParseOptExtra` 这对泛型函数如何在「JSON 世界」和「Go 类型世界」之间做翻译。
- 写出 HTTP / FTP / BT / ED2K 各协议 `ReqExtra` / `OptsExtra` 的用途对照表。
- 解释 `CreateTaskBatch` / `CreateTaskBatchItem` 批量模型的字段（含本轮新增的 `Group` / `GroupName` 与 `Rid`）与「rid 优先、成员 opts 整体替换、部分成功」的批量规则。
- 通过真实单元测试验证 `CalcSize` 的选择计算逻辑，并亲手序列化一份 HTTP `ReqExtra` 和一份分组批量请求。

本讲是整个学习手册的「词汇表」：后续讲 Task 状态机、Downloader 主链路、各协议实现时，全部用这里的名词说话。把这一讲吃透，后面读任何源码都会顺畅得多。

## 2. 前置知识

阅读本讲前，你只需要具备：

1. **Go 基础语法**：结构体、指针接收者、`any`（即 `interface{}`）、泛型函数（`func F[E any](...)`）。
2. **JSON 序列化常识**：`encoding/json` 的 tag（`json:"xxx"`、`json:"xxx,omitempty"`）、`json.Marshal` 会把 `map[string]string` 的键按字典序输出、把 `any` 反序列化成 `map[string]any`。
3. **已完成 u1-l3（仓库地图）**：知道 `pkg/` 放公共模型与库、`internal/` 放协议实现。本讲的模型全部在 `pkg/base` 与 `pkg/protocol` 中，它们是公开 API 的一部分，稳定性要求最高。

两个需要先建立直觉的概念：

- **解析（Resolve）与下载（Download）是两个阶段**：Gopeed 先用一次轻量探测搞清楚「要下什么」（文件名、大小、能不能分段、包含哪些文件），把结果装进 `Resource`；用户确认后再开始真正的传输。`Request` 是解析阶段的输入，`Resource` 是解析阶段的输出，`Options` 则贯穿两个阶段，描述「用户想怎么下」。
- **边界即类型转换处**：同一个 `Request` 既会以 Go 结构体形式在核心内部流转，也会以 JSON 形式穿过 REST API、Flutter 前端、JS 扩展。`Extra any` 就是这两种表示法的交汇点，本讲 4.4 会专门讲它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/base/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go) | 领域模型核心：`Request` / `Resource` / `FileInfo` / `Options`、`ParseReqExtra` / `ParseOptExtra`、`CreateTaskBatch` / `CreateTaskBatchItem`，以及下一讲的 `DownloaderStoreConfig` |
| [pkg/protocol/http/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/http/model.go) | HTTP 协议的公开参数模型：`ReqExtra`（Method/Header/Body）、`OptsExtra`（连接数、校验和等）、`Stats` |
| [pkg/protocol/ftp/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/ftp/model.go) | FTP 协议参数模型：`ReqExtra`（TLS 模式）、`OptsExtra`（并行登录数等） |
| [pkg/protocol/bt/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/bt/model.go) | BT 协议参数模型：`ReqExtra`（tracker 列表）与做种统计快照 |
| [pkg/protocol/ed2k/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/ed2k/model.go) | ED2K 协议模型：只有统计结构，**没有** `ReqExtra` / `OptsExtra`（对照组） |
| [pkg/base/model_test.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model_test.go) | `pkg/base` 的现有测试，学习表驱动测试写法的范本 |
| [pkg/util/json.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/util/json.go) | `MapToStruct`：`Extra` 类型转换的底层实现 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go) | 三要素的真实使用现场：`Resolve`、`createResolved`、`CreateDirectBatchWithResult` 等 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go) | `ResolveResult`、`TaskGroup`、`CreateTaskBatchResult`：批量创建的返回值与分组元数据 |
| [_docs/task-batch-api.md](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/_docs/task-batch-api.md) | 批量创建 API 的行为规格文档（rid 优先、opts 替换、部分成功语义的权威描述） |

注意一个重要分层：`pkg/protocol` 只放「用户可配置的公开参数模型」，而协议的具体实现（fetcher、连接管理、解析逻辑）在 `internal/protocol` 下。这是 u1-l3 讲过的「pkg 稳定、internal 可自由重构」原则的体现。

## 4. 核心概念与源码讲解

本讲的五个最小模块：

1. Request 与 RequestProxy —— 「我想下载什么」
2. Resource 与 FileInfo —— 「服务器说那里有什么」
3. Options 与 SelectFiles —— 「我要怎么下载它」
4. 协议 Extra 模型 —— 「每个协议私有的补充参数」
5. CreateTaskBatch 批量请求模型 —— 「一次提交一批三要素」

### 4.1 Request 与 RequestProxy

#### 4.1.1 概念说明

`Request` 回答的问题是：**从哪里下载、以什么身份下载**。它是一次下载的起点，包含地址、协议私有参数、任务标签和代理设置。

为什么需要单独一个结构体而不是只传一个 URL 字符串？因为真实下载远不止 URL：

- 磁力链接、`.torrent` 文件路径、`ftp://` 目录都可以是「地址」；
- HTTP 下载可能要指定请求方法、请求头、请求体；
- 某些任务需要走专属代理（比如为某个站点专门配置的代理）；
- 任务需要打标签（Labels）方便前端筛选。

#### 4.1.2 核心流程

一个 `Request` 的生命周期：

```text
用户输入 / REST API / JS 扩展
        │
        ▼
Downloader.Resolve(req)          ← ensureRequestRawURL 保证 RawURL 非空
        │
        ▼
按 URL 前缀/后缀路由到某个协议 Fetcher
        │
        ▼
fetcher.Resolve(req)             ← 各协议解析出 Resource
  （HTTP 在这里读取 req.Extra 里的 Method/Header/Body，
     并可能在 Patch 时改写 req.URL）
```

关键点：`RawURL` 与 `URL` 是两个不同的字段——`RawURL` 保存用户最初输入的地址，`URL` 是实际发请求用的地址，后者可能被扩展或 `Patch` 改写过。`URL` 被改写后 `RawURL` 仍能找回「用户当初给的是什么」。

#### 4.1.3 源码精读

Request 结构体定义在 [pkg/base/model.go:L17-L28](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L17-L28)：

```go
// Request download request
type Request struct {
	RawURL string `json:"rawUrl"`
	URL    string `json:"url"`
	Extra  any    `json:"extra"`
	// Labels is used to mark the download task
	Labels map[string]string `json:"labels"`
	// proxy is special proxy config for request
	Proxy *RequestProxy `json:"proxy"`
	// SkipVerifyCert is the flag that skip verify cert
	SkipVerifyCert bool `json:"skipVerifyCert"`
}
```

字段逐个说明：

| 字段 | 含义 |
| --- | --- |
| `RawURL` | 用户输入的原始地址（未经过扩展改写、未经过重定向） |
| `URL` | 实际请求地址；HTTP 重定向、扩展改写、`Patch` 都可能更新它 |
| `Extra` | 协议私有参数，类型为 `any`，详见 4.4 |
| `Labels` | 任务标签键值对，供前端分组/筛选展示 |
| `Proxy` | 本请求专属代理，见下文 |
| `SkipVerifyCert` | 跳过 TLS 证书校验（自签名证书场景） |

`RawURL` 的兜底逻辑在 [pkg/download/downloader.go:L538-L545](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L538-L545)：`Resolve` 与 `createDirect` 入口都会调用 `ensureRequestRawURL`，当 `RawURL` 为空时用 `URL` 回填，保证「原始地址」这个概念永远有值：

```go
func ensureRequestRawURL(req *base.Request) {
	if req == nil {
		return
	}
	if req.RawURL == "" {
		req.RawURL = req.URL
	}
}
```

`URL` 被改写的实例可以看 HTTP fetcher 的 Patch 实现 [internal/protocol/http/fetcher.go:L2380-L2386](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/protocol/http/fetcher.go#L2380-L2386)：换 URL 的同时清空重定向缓存，让后续请求直连新地址。

请求校验非常克制，只有一条规则，见 [pkg/base/model.go:L57-L62](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L57-L62)（URL 非空）——协议格式合法性的校验留给各协议 Fetcher 自己完成。

代理模型分三档，见 [pkg/base/model.go:L64-L81](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L64-L81)：

```go
const (
	RequestProxyModeFollow RequestProxyMode = "follow" // 跟随全局代理设置
	RequestProxyModeNone   RequestProxyMode = "none"   // 本请求强制直连
	RequestProxyModeCustom RequestProxyMode = "custom" // 使用下方自定义代理
)

type RequestProxy struct {
	Mode   RequestProxyMode `json:"mode"`
	Scheme string           `json:"scheme"`
	Host   string           `json:"host"`
	Usr    string           `json:"usr"`
	Pwd    string           `json:"pwd"`
}
```

`ToHandler` 方法（[pkg/base/model.go:L83-L93](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L83-L93)）把配置翻译成 Go 标准库 `http.Transport` 认识的代理函数：只有 `custom` 模式且 `Scheme`/`Host` 都齐全才生效，`follow`/`none` 返回 `nil` 交给上层（全局配置）处理。这种「请求级配置优先、缺省回落全局」的三态设计在 Gopeed 里反复出现。

另外注意 [pkg/base/model.go:L30-L55](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L30-L55) 的 `SetLabels` / `PutLabel` / `DelLabel` 三个方法都标注了 `Deprecated`——它们是给旧版 JS 扩展保留的兼容入口，新代码应直接操作 `Labels` 字段。读源码时看到 `Deprecated` 注释不要跳过，它往往记录着 API 演化的历史。

#### 4.1.4 代码实践

**实践目标**：验证 `Request` 的 JSON 形态与 `Validate` 行为，体会「Go 结构体 ↔ JSON」的映射。

**操作步骤**：

1. 在仓库根目录新建目录 `learn/u2l1/`（与 `pkg` 平级，仅用于练习，不进入正式源码）。
2. 新建文件 `learn/u2l1/request_learn_test.go`，内容如下（示例代码）：

```go
package u2l1

import (
	"encoding/json"
	"testing"

	"github.com/GopeedLab/gopeed/pkg/base"
)

func TestRequestBasics(t *testing.T) {
	req := &base.Request{
		URL:    "https://example.com/file.iso",
		Labels: map[string]string{"kind": "movie"},
		Proxy:  &base.RequestProxy{Mode: base.RequestProxyModeCustom, Scheme: "http", Host: "127.0.0.1:7890"},
	}
	// 1. 校验：URL 非空即合法
	if err := req.Validate(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// 2. 空 URL 必须报错
	if err := (&base.Request{}).Validate(); err == nil {
		t.Fatal("empty url should be invalid")
	}
	// 3. 观察它穿过 REST 边界时的 JSON 形态
	data, _ := json.Marshal(req)
	t.Logf("request json: %s", data)
	// 4. 模拟 RawURL 兜底：入口处 RawURL 为空时由 URL 回填
	if req.RawURL == "" {
		req.RawURL = req.URL
	}
	t.Logf("rawUrl after fallback: %s", req.RawURL)
}
```

3. 在仓库根目录运行：`go test ./learn/u2l1/ -run TestRequestBasics -v`

**需要观察的现象**：日志里打印的 JSON 中，`rawUrl` 与 `url` 的值、`proxy.mode` 的取值。

**预期结果**：JSON 形如 `{"rawUrl":"","url":"https://example.com/file.iso","extra":null,"labels":{"kind":"movie"},"proxy":{"mode":"custom","scheme":"http","host":"127.0.0.1:7890","usr":"","pwd":""},"skipVerifyCert":false}`；`rawUrl` 兜底后等于 URL。（以本地实际运行输出为准，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`RequestProxyModeNone` 和不设置 `Proxy`（`nil`）有什么区别？

**答案**：`Proxy == nil` 表示「不指定，按默认逻辑（通常是跟随全局代理配置）处理」；`Mode = none` 是显式声明「这个请求必须直连，即使全局开了代理」。`ToHandler` 对两者都返回 `nil`，但上层调度会把 `none` 识别为「禁止代理」而不是「未设置」，语义不同。

**练习 2**：为什么 `RawURL` 和 `URL` 要分成两个字段？

**答案**：`URL` 会被重定向跟踪、JS 扩展改写、`Patch` 接口更新为「真正发请求的地址」；而任务列表 UI、扩展触发、日志排查都需要回溯「用户当初输入了什么」。`RawURL` 就是这份不可变的原始输入，`ensureRequestRawURL` 保证它一定有值。

**练习 3**：`Request.Validate` 为什么只校验 URL 非空，而不校验 scheme 合法性？

**答案**：因为协议路由（scheme 匹配、文件后缀匹配）是 `FetcherManager` 的职责（u3-l2 会讲），`base` 包作为公共模型层不应预设「合法地址」的协议集合——例如本地 `.torrent` 文件路径也能作为请求地址。分层校验让模型层保持稳定。

### 4.2 Resource 与 FileInfo

#### 4.2.1 概念说明

`Resource` 是**解析阶段的产物**，回答的问题是：**地址背后到底有什么**。解析完成后，用户在 UI 里看到的「文件名、总大小、包含哪些文件、能否续传」全部来自这个结构体。

`FileInfo` 是 `Resource.Files` 里的元素，描述资源中的一个文件。多文件资源（BT 种子、FTP 目录、HLS 分片集合）会把每个文件列成一个 `FileInfo`。

#### 4.2.2 核心流程

```text
Request ──fetcher.Resolve()──▶ Resource
                                  │
                      ┌───────────┼────────────────┐
                      ▼           ▼                ▼
                Name/Size/Range  Files[]          Hash
               （单文件即文件名 （多文件时 Name     （BT 种子的
                 多文件即目录名）  是目录名）          infohash）
                      │
                      ▼
     用户在 UI 勾选文件 ──▶ Options.SelectFiles
                      │
                      ▼
     Resource.CalcSize(SelectFiles) 重算总大小
```

`CalcSize` 的计算规则可以写成公式。设文件总数为 \( n \)，被选中的下标集合为 \( S \)（当 `selectFiles` 为空时 \( S = \{0,1,\dots,n-1\} \)，即全选）：

\[
\text{Size} = \sum_{i \in S} \text{Files}[i].\text{Size}
\]

注意这是一个**覆盖式赋值**（`r.Size = size`）：每次调用都会用当前选择重算并覆盖 `Size`，而不是累加。所以「先选 3 个文件再改成选 1 个」不会残留旧值。

#### 4.2.3 源码精读

Resource 定义在 [pkg/base/model.go:L95-L105](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L95-L105)：

```go
// Resource download resource
type Resource struct {
	// if name is not empty, the resource is a folder and the name is the folder name
	Name string `json:"name"`
	Size int64  `json:"size"`
	// is support range download
	Range bool `json:"range"`
	// file list
	Files []*FileInfo `json:"files"`
	Hash  string      `json:"hash"`
}
```

| 字段 | 含义 | 典型来源 |
| --- | --- | --- |
| `Name` | 资源名。单文件资源是文件名；多文件资源是**目录名**（注释明确说明这一语义） | HTTP 从 `Content-Disposition`/URL 解析；BT 从种子元数据 |
| `Size` | 总大小（字节），由 `CalcSize` 按当前选择维护 | 选中文件大小之和 |
| `Range` | 是否支持分段（Range）下载——决定 HTTP 能否开多连接 | HTTP 探测响应的 `Accept-Ranges` |
| `Files` | 文件列表，至少一项（`Validate` 强制） | 各协议解析 |
| `Hash` | 资源哈希标识 | BT 的 infohash；HTTP 通常为空 |

`CalcSize` 实现在 [pkg/base/model.go:L119-L127](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L119-L127)：

```go
func (r *Resource) CalcSize(selectFiles []int) {
	var size int64
	for i, file := range r.Files {
		if len(selectFiles) == 0 || slices.Contains(selectFiles, i) {
			size += file.Size
		}
	}
	r.Size = size
}
```

两个细节：`len(selectFiles) == 0` 代表全选（与 4.3 的 `InitSelectFiles` 呼应）；用 `slices.Contains` 做下标成员判断，时间复杂度是 \( O(n \cdot |S|) \)，对文件数上万的大型种子也够用（选择操作频率极低）。

`Resource.Validate`（[pkg/base/model.go:L107-L117](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L107-L117)）要求 `Files` 非空且每个文件 `Name` 非空——即一个 `Resource` 至少描述一个可命名文件，不允许「空资源」。

FileInfo 定义在 [pkg/base/model.go:L129-L136](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L129-L136)：

```go
type FileInfo struct {
	Name  string     `json:"name"`
	Path  string     `json:"path"`
	Size  int64      `json:"size"`
	Ctime *time.Time `json:"ctime"`

	Req *Request `json:"req"`
}
```

最值得琢磨的是 `Req *Request` 这个字段：**每个文件可以携带自己的请求**。普通 HTTP/BT 下载用不到它，但当 JS 扩展在内存里「凭空产出」一个多文件资源时（u9 的 Blob 机制），每个文件各自对应不同的获取方式，就需要逐文件描述。Downloader 在 [pkg/download/downloader.go:L547-L560](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L547-L560) 的 `ensureResourceRequestRawURLs` 中专门为这些内嵌请求回填 `RawURL`，测试工具 `cmd/extmock`（[cmd/extmock/main.go:L283-L309](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/extmock/main.go#L283-L309)）就是靠 `file.Req` 把模拟资源逐个转成真实任务的。

`Resource` 是 `Resolve` 的直接产出，见 [pkg/download/downloader.go:L562-L598](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L562-L598)：`Downloader.Resolve` 优先让扩展产出资源（`triggerOnResolve`），否则路由到协议 Fetcher 解析，最终都装进 `ResolveResult.Res` 返回给调用方。

#### 4.2.4 代码实践

**实践目标**：用单元测试验证 `CalcSize` 在「全选 / 部分选择 / 空选择」三种情况下的行为。

**操作步骤**：

1. 新建 `learn/u2l1/resource_learn_test.go`（示例代码）：

```go
package u2l1

import (
	"testing"

	"github.com/GopeedLab/gopeed/pkg/base"
)

func TestResourceCalcSize(t *testing.T) {
	res := &base.Resource{
		Name: "ubuntu",
		Files: []*base.FileInfo{
			{Name: "ubuntu.iso", Path: "ubuntu", Size: 100},
			{Name: "readme.txt", Path: "ubuntu", Size: 900},
			{Name: "hash.nfo", Path: "ubuntu", Size: 50},
		},
	}
	if err := res.Validate(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	cases := []struct {
		name        string
		selectFiles []int
		want        int64
	}{
		{"empty selects all", nil, 1050},   // 空切片 = 全选
		{"single select", []int{1}, 900},   // 只选第 2 个
		{"multi select", []int{0, 2}, 150}, // 选第 1、3 个
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res.CalcSize(c.selectFiles)
			if res.Size != c.want {
				t.Fatalf("CalcSize(%v) = %d, want %d", c.selectFiles, res.Size, c.want)
			}
		})
	}
}
```

2. 运行：`go test ./learn/u2l1/ -run TestResourceCalcSize -v`

**需要观察的现象**：三个子测试是否全部通过；`res.Size` 是否为覆盖式更新而非累加。

**预期结果**：三个子测试全部 PASS。可以再加一个「重复调用」用例：先 `CalcSize(nil)` 再 `CalcSize([]int{1})`，确认 `Size` 从 1050 变为 900 而不是 1950。（待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：一个 BT 种子解析出的 `Resource`，`Name` 是 `"BigBuckBunny"`、`Files` 有 3 项。用户在 UI 上看到的名字从哪来？下载完成后磁盘上的目录结构是什么？

**答案**：UI 顶层显示 `Name`（目录名）。下载完成后会在下载目录下创建 `BigBuckBunny/` 目录，`Files` 中各项按 `Path + Name` 落盘。单文件 HTTP 资源则相反：`Name` 直接是文件名，不建目录——这就是注释「if name is not empty, the resource is a folder」配合「文件数是否为 1」共同决定的两种形态。

**练习 2**：`Resource.Range` 为 `false` 时，HTTP 下载会受什么影响？

**答案**：无法分段下载，HTTP Fetcher 只能用单连接顺序下载（u5-l3 会讲回退逻辑）。`Range` 本质上来自服务器对 `Range` 请求头的支持能力（`Accept-Ranges` / 206 响应），BT/ED2K 等基于分块交换的协议则天然支持按 piece 续传。

**练习 3**：`FileInfo.Req` 为什么不放在 `Resource` 上统一给一个？

**答案**：因为扩展产出的多文件资源里，每个文件可能来自**不同的地址**（甚至不同的请求方法/头），一个资源级 `Request` 表达不了这种一对多的映射。而普通协议下载中该字段保持 `nil`，各 Fetcher 沿用 `FetcherMeta.Req` 即可，不增加负担。

### 4.3 Options 与 SelectFiles

#### 4.3.1 概念说明

`Options` 回答的问题是：**用户希望怎么完成这次下载**。下载到哪、叫什么名字、选哪些文件、用多少连接——这些都属于「下载意图」，与「资源本身长什么样」（`Resource`）是正交的两个维度。

`SelectFiles` 是其中最值得精读的字段：它是一个**下标数组**，指向 `Resource.Files` 的位置。空切片有特殊语义——代表「全选」。

#### 4.3.2 核心流程

`Options` 与 `SelectFiles` 在建任务主链路上的协作（核心逻辑在 [pkg/download/downloader.go:L868-L913](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L868-L913) 的 `createResolved`，它同时服务于单任务创建 `CreateWithOptions` 和 4.5 的批量 `rid` 成员）：

```text
用户提交 Options
      │
      ▼
opts.Clone()                    ← 深拷贝，隔离调用方后续修改
      │
      ▼
校验 SelectFiles 每个下标 ∈ [0, len(Files))   ← 越界直接报错
      │
      ▼
taskOpts.InitSelectFiles(len(res.Files))      ← 空选择展开为 [0,1,...,n-1]
      │
      ▼
res.CalcSize(fetcher.Meta().Opts.SelectFiles) ← 按最终选择重算资源大小
      │
      ▼
fetcher.Meta().Opts = taskOpts  ← 固化为任务的选项
```

这套流程把「空 = 全选」的约定在入口处**规范化**：进入 Fetcher 之后，`SelectFiles` 永远是显式下标列表，协议实现无需再处理空值分支。

#### 4.3.3 源码精读

Options 定义在 [pkg/base/model.go:L138-L156](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L138-L156)：

```go
// Options for download
type Options struct {
	// Size is an optional HTTP download size estimate. HTTP metadata and clean EOF take precedence.
	// Other protocols ignore it.
	Size int64 `json:"size,omitempty"`
	// Download file name
	Name string `json:"name"`
	// Download file path
	Path string `json:"path"`
	// Use this path as the default download directory after creation succeeds
	AsDefaultPath bool `json:"asDefaultPath,omitempty"`
	// SkipAutoCategorize keeps the base download directory of this task instead
	// of routing it to the category directory of its resolved file
	SkipAutoCategorize bool `json:"skipAutoCategorize,omitempty"`
	// Select file indexes to download
	SelectFiles []int `json:"selectFiles"`
	// Extra info for specific fetcher
	Extra any `json:"extra"`
}
```

| 字段 | 含义 | 备注 |
| --- | --- | --- |
| `Size` | HTTP 下载的大小估计值 | 仅 HTTP 使用；真实元数据（响应头）与干净 EOF 优先 |
| `Name` | 覆盖默认文件名 | 空则使用 `Resource.Name` |
| `Path` | 下载目录 | 受全局目录白名单约束（u10-l1 讲 `decideDownloadDir`） |
| `AsDefaultPath` | 建任务成功后把该目录记住为默认目录 | |
| `SkipAutoCategorize` | 跳过按扩展名自动归类到分类目录 | 与 u10-l1 的自动分类机制配合；4.5 会看到分组批量会强制置 true |
| `SelectFiles` | 要下载的文件下标列表 | 空代表全选，见 `InitSelectFiles` |
| `Extra` | 协议私有选项，类型 `any` | 与 `Request.Extra` 对称，详见 4.4 |

`InitSelectFiles` 在 [pkg/base/model.go:L158-L166](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L158-L166)：

```go
func (o *Options) InitSelectFiles(fileSize int) {
	// if selectFiles is empty, select all files
	if len(o.SelectFiles) == 0 {
		o.SelectFiles = make([]int, fileSize)
		for i := range fileSize {
			o.SelectFiles[i] = i
		}
	}
}
```

`Clone`（[pkg/base/model.go:L168-L170](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L168-L170)）借助 `util.DeepClone` 做 JSON 往返式深拷贝。Downloader 在创建任务前总是先 Clone（[pkg/download/downloader.go:L888](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L888)），避免调用方在任务启动后修改手里的 `Options` 波及任务内部状态——这是「边界处拷贝」防御性编程的典型做法。

三要素协作的现场在 `createResolved` 中，见 [pkg/download/downloader.go:L892-L903](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L892-L903)：

```go
	if res := fetcher.Meta().Res; res != nil {
		for _, index := range taskOpts.SelectFiles {
			if index < 0 || index >= len(res.Files) {
				return "", fmt.Errorf("invalid selected file index: %d", index)
			}
		}
		taskOpts.InitSelectFiles(len(res.Files))
	}
	fetcher.Meta().Opts = taskOpts
	if res := fetcher.Meta().Res; res != nil {
		res.CalcSize(fetcher.Meta().Opts.SelectFiles)
	}
```

先校验、再规范化、最后重算大小——三行代码分别守护了安全性、语义一致性和数据准确性。这种「入口归一化」的风格在 Gopeed 源码里随处可见。

另外，[pkg/base/model.go:L202-L213](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L202-L213) 的 `CreateTaskBatch` / `CreateTaskBatchItem` 是「批量版三要素」的请求体，本讲 4.5 专门展开。

#### 4.3.4 代码实践

**实践目标**：验证 `InitSelectFiles` 的全选展开与 `Clone` 的深拷贝隔离性。

**操作步骤**：

1. 新建 `learn/u2l1/options_learn_test.go`（示例代码）：

```go
package u2l1

import (
	"reflect"
	"testing"

	"github.com/GopeedLab/gopeed/pkg/base"
)

func TestOptionsSelectFilesAndClone(t *testing.T) {
	// 1. 空选择展开为全选
	opts := &base.Options{Name: "a.iso", Path: "/tmp"}
	opts.InitSelectFiles(3)
	if !reflect.DeepEqual(opts.SelectFiles, []int{0, 1, 2}) {
		t.Fatalf("InitSelectFiles = %v, want [0 1 2]", opts.SelectFiles)
	}
	// 2. 非空选择保持原样
	opts2 := &base.Options{SelectFiles: []int{1}}
	opts2.InitSelectFiles(3)
	if !reflect.DeepEqual(opts2.SelectFiles, []int{1}) {
		t.Fatalf("non-empty select must be kept, got %v", opts2.SelectFiles)
	}
	// 3. Clone 之后修改副本不影响原件
	clone := opts.Clone()
	clone.SelectFiles[0] = 99
	clone.Name = "changed"
	if opts.SelectFiles[0] != 0 || opts.Name != "a.iso" {
		t.Fatalf("Clone is not deep: original = %+v", opts)
	}
}
```

2. 运行：`go test ./learn/u2l1/ -run TestOptionsSelectFilesAndClone -v`

**需要观察的现象**：第 3 步修改 `clone` 后原件是否纹丝不动；如果把 `Clone` 换成直接赋值 `clone := opts`，测试会发生什么。

**预期结果**：三个断言全部通过。做对照实验时，浅拷贝版本会因 slice 底层数组共享而导致 `opts.SelectFiles[0]` 变成 99。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Options.SelectFiles` 用「下标」而不是「文件名列表」？

**答案**：BT 种子里允许重名文件（不同目录下同名），文件名不是唯一键；而下标与 `Resource.Files` 的顺序一一对应，天然唯一。代价是依赖列表稳定性——所以 `Resource.Files` 一旦生成不可重排，协议实现里重算资源（如 BT 的 `updateRes`）必须保持顺序。

**练习 2**：`Options.Size` 为什么放在通用 `Options` 里而不是 HTTP 的 `OptsExtra`？

**答案**：这是一个 API 设计取舍。`Size` 是「创建任务时的一次性提示」，需要被 Downloader 层在 `createResolved` 中读取并直接 `Patch` 给 fetcher（[pkg/download/downloader.go:L904-L910](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L904-L910)），放进通用结构可以让 Downloader 不必理解每个协议的 Extra 类型；注释同时声明「其他协议忽略它」来限定作用域。

**练习 3**：如果用户提交的 `SelectFiles` 含越界下标 `-1`，会在哪一步、以什么形式失败？

**答案**：在 `createResolved` 的下标校验循环中（[pkg/download/downloader.go:L892-L897](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L892-L897)）直接返回错误 `invalid selected file index: -1`，任务不会被创建。这是「尽早失败」原则：错误下标如果漏进 Fetcher，可能导致越界 panic 而不是可读的错误。

### 4.4 协议 Extra 模型

#### 4.4.1 概念说明

`Request.Extra` 和 `Options.Extra` 都声明为 `any`——这是整个模型里最「动态」的部分。它存在的原因：**不同协议需要的私有参数完全不同，但公共模型又必须统一**。HTTP 要 Method/Header/Body，FTP 要 TLS 模式，BT 要 tracker 列表，如果全部展开成 `Request` 的字段，公共模型会被协议细节污染。

于是设计变成：

- 公共模型只放 `Extra any`；
- 每个协议在 `pkg/protocol/<name>/model.go` 定义自己的 `ReqExtra` / `OptsExtra`；
- 需要读取时，通过泛型函数把 `any` 转换成具体类型。

**问题的核心在边界**。当 `Request` 穿过 REST API 或持久化存储再读回来时，`Extra` 的具体类型已经丢失——JSON 反序列化只能得到 `map[string]any`。`ParseReqExtra` / `ParseOptExtra` 就是负责「把 `map[string]any` 还原成具体协议类型」的翻译器。

#### 4.4.2 核心流程

```text
Go 调用方                       REST/存储/扩展 边界
Extra = &http.ReqExtra{...} ──json──▶ Extra = map[string]any{...}
       │                                      │
       │ 直接类型断言即可                       │ 需要「翻译」
       ▼                                      ▼
   使用字段                ParseReqExtra[E](req)
                                          │
                            ┌─────────────┴─────────────┐
                            ▼                           ▼
                 Extra 已是 *E？→ 直接返回     否则 MapToStruct：
                                            json.Marshal(map) →
                                            json.Unmarshal(&E)
                                            并把 Extra 替换为 &E
```

`ParseReqExtra` 是**幂等**的：如果 `Extra` 已经是目标类型 `*E` 就直接返回，不做任何转换。这意味着调用方可以放心地在任何入口反复调用它，转换只发生一次。

#### 4.4.3 源码精读

两个泛型函数在 [pkg/base/model.go:L172-L200](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L172-L200)：

```go
func ParseReqExtra[E any](req *Request) error {
	if req.Extra == nil {
		return nil
	}
	if _, ok := req.Extra.(*E); ok {
		return nil
	}
	var t E
	if err := util.MapToStruct(req.Extra, &t); err != nil {
		return err
	}
	req.Extra = &t
	return nil
}
```

四个分支：`nil` 直接放过（没有额外参数）；已是 `*E` 幂等返回（Go 进程内调用方直接传结构体的场景）；否则经 `MapToStruct` 转换；转换失败返回错误（JSON 结构对不上）。`ParseOptExtra` 与之完全对称，作用于 `Options.Extra`。

底层 `MapToStruct` 在 [pkg/util/json.go:L5-L14](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/util/json.go#L5-L14)，实现非常直白——一次 Marshal 加一次 Unmarshal：

```go
func MapToStruct(s any, v any) error {
	if s == nil {
		return nil
	}
	b, err := json.Marshal(s)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}
```

**各协议 Extra 对照表**（本讲学习目标之一）：

| 协议 | ReqExtra（请求侧） | OptsExtra（选项侧） | 定义位置 |
| --- | --- | --- | --- |
| HTTP | `Method`（请求方法）、`Header`（请求头）、`Body`（请求体） | `Connections`（连接数）、`AutoTorrent`、`DeleteTorrentAfterDownload`、`AutoExtract`、`ArchivePassword`、`DeleteAfterExtract`、`Checksum`（完成后校验和） | [pkg/protocol/http/model.go:L9-L13](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/http/model.go#L9-L13) 与 [L81-L98](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/http/model.go#L81-L98) |
| FTP | `TLS`（`none`/`explicit`/`implicit`，见 [pkg/protocol/ftp/model.go:L13-L17](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/ftp/model.go#L13-L17)） | `Connections`（并行登录上限）+ 与 HTTP 相同的自动化字段（[L19-L36](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/ftp/model.go#L19-L36)） | pkg/protocol/ftp/model.go |
| BT | `Trackers`（tracker URL 列表，[pkg/protocol/bt/model.go:L5-L7](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/bt/model.go#L5-L7)） | **无**——文件选择用通用 `Options.SelectFiles` | pkg/protocol/bt/model.go |
| ED2K | **无**（[pkg/protocol/ed2k/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/ed2k/model.go) 全文件只有统计结构） | **无** | pkg/protocol/ed2k/model.go |
| HLS | 镜像 HTTP 的 `Method/Header/Body`（[internal/protocol/hls/fetcher.go:L24-L30](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/protocol/hls/fetcher.go#L24-L30)，因创建对话框给 m3u8 附带 HTTP 风格参数） | 无独立模型 | internal/protocol/hls |

注意两个不对称：

1. **HTTP/FTP 的 OptsExtra 有大量相同字段**（AutoTorrent、AutoExtract 等）但**没有抽公共结构**——因为这些「下载完成后动作」实际由 Downloader 层按协议名分别消费（u10 讲），字段重复是刻意保持各协议模型独立演化的结果。
2. **`*bool` 三态模式**：`AutoTorrent *bool` 等字段用指针区分「未设置（用全局配置）/显式 true/显式 false」，与 4.1 的代理三态是同一思想。

`OptsExtra` 中的 `ChecksumOption` 值得单独一看（[pkg/protocol/http/model.go:L15-L79](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/http/model.go#L15-L79)）：`Validate` 方法校验算法名（大小写不敏感，允许 `sha-1` 别名）、哈希长度必须精确等于 32/40/64（md5/sha1/sha256）、且必须是合法十六进制。配套测试 [pkg/protocol/http/model_test.go:L20-L162](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/http/model_test.go#L20-L162) 用表驱动覆盖了 off-by-one 长度、非 hex 字符等边界——是学习该项目测试风格的好范本。

最后看协议侧的真实调用。HTTP Fetcher 在 Resolve 入口转换请求侧 Extra（[internal/protocol/http/fetcher.go:L419-L420](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/protocol/http/fetcher.go#L419-L420)），BT Fetcher 同样在 Resolve 中解析 tracker 列表（[internal/protocol/bt/fetcher.go:L748](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/protocol/bt/fetcher.go#L748)）。最能说明「为什么需要 Parse」的是持久化恢复路径：任务从 bbolt 读回时 `Extra` 是 JSON 反序列化出的 map，所以 HTTP 的 FetcherManager 在 Restore 时必须重新调用一次 `ParseReqExtra` / `ParseOptExtra`（[internal/protocol/http/fetcher_manager.go:L161-L162](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/protocol/http/fetcher_manager.go#L161-L162)），FTP 的 Restore 也一样（[internal/protocol/ftp/fetcher_manager.go:L95-L98](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/protocol/ftp/fetcher_manager.go#L95-L98)）。HTTP 的 `Patch` 甚至要做**字段级合并**而非整体替换（[internal/protocol/http/fetcher.go:L2387-L2413](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/internal/protocol/http/fetcher.go#L2387-L2413)），避免只改 Method 时丢掉已有 Header。

#### 4.4.4 代码实践

**实践目标**：填充 HTTP `ReqExtra` 并打印 JSON 序列化结果；再模拟「穿过 JSON 边界」的过程，用 `ParseReqExtra` 把 `map[string]any` 还原成具体类型。

**操作步骤**：

1. 新建 `learn/u2l1/extra_learn_test.go`（示例代码）。注意 HTTP 协议包名是 `http`，必须起别名避免与标准库冲突（项目内部统一用 `fhttp`）：

```go
package u2l1

import (
	"encoding/json"
	"testing"

	"github.com/GopeedLab/gopeed/pkg/base"
	fhttp "github.com/GopeedLab/gopeed/pkg/protocol/http"
)

func TestHttpReqExtra(t *testing.T) {
	// 1. Go 侧直接构造具体类型
	extra := &fhttp.ReqExtra{
		Method: "POST",
		Header: map[string]string{
			"User-Agent": "gopeed-tutorial",
			"Accept":     "application/json",
		},
		Body: "k=v",
	}
	data, err := json.Marshal(extra)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("ReqExtra json: %s", data)

	// 2. 模拟穿过 REST/存储边界：JSON → any 得到的是 map
	var asAny any
	if err := json.Unmarshal(data, &asAny); err != nil {
		t.Fatal(err)
	}
	req := &base.Request{URL: "https://example.com/api", Extra: asAny}

	// 3. 幂等转换成具体类型
	if err := base.ParseReqExtra[fhttp.ReqExtra](req); err != nil {
		t.Fatal(err)
	}
	parsed, ok := req.Extra.(*fhttp.ReqExtra)
	if !ok {
		t.Fatalf("extra type = %T, want *http.ReqExtra", req.Extra)
	}
	if parsed.Method != "POST" || parsed.Body != "k=v" || parsed.Header["Accept"] != "application/json" {
		t.Fatalf("parsed extra mismatch: %+v", parsed)
	}

	// 4. 验证幂等性：再次 Parse 不会破坏已转换的类型
	if err := base.ParseReqExtra[fhttp.ReqExtra](req); err != nil {
		t.Fatal(err)
	}
	if _, ok := req.Extra.(*fhttp.ReqExtra); !ok {
		t.Fatal("second Parse must keep the concrete type")
	}

	// 5. 反例：结构不匹配时报错而非静默
	bad := &base.Request{URL: "x", Extra: map[string]any{"method": 123}}
	if err := base.ParseReqExtra[fhttp.ReqExtra](bad); err == nil {
		t.Fatal("mismatched extra must fail")
	}
}
```

2. 运行：`go test ./learn/u2l1/ -run TestHttpReqExtra -v`

**需要观察的现象**：第 1 步打印的 JSON 字段顺序与 map 键顺序；第 3、4 步类型断言；第 5 步的错误信息。

**预期结果**：

- JSON 输出为 `{"method":"POST","header":{"Accept":"application/json","User-Agent":"gopeed-tutorial"},"body":"k=v"}`——结构体字段按声明顺序（method→header→body），map 键按字典序（Accept 在 User-Agent 前）。
- 第 5 步报错：`json: cannot unmarshal number into Go struct field ReqExtra.method of type string`（`MapToStruct` 的 Unmarshal 阶段拒绝类型不匹配）。（待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：如果不用 `Extra any`，而是给 `Request` 加 `Method string`、`Header map[string]string`、`Trackers []string` 等字段，会出什么问题？

**答案**：公共模型被协议细节污染：每接入一个新协议都要改 `pkg/base`（不稳定 API）；HTTP 字段对 BT 无意义却随任务持久化、随 API 传输；扩展和前端也要面对一堆无关字段。`Extra any` 把「协议私有」的责任推回协议包，`base` 保持稳定——这正是「pkg 稳定、internal 自由」分层的动机。

**练习 2**：`ParseReqExtra` 为什么要把转换结果**写回** `req.Extra`（`req.Extra = &t`），而不是只返回转换值？

**答案**：写回让函数幂等且免重复转换——同一请求流转过程中可能被多次「准备」（Restore、Patch、扩展触发），第一次转换后再次调用直接走 `*E` 快速分支。若只返回值，每个调用点都要各自保存一份局部变量，`Extra` 字段里永远是 map，序列化/比较语义不一致。

**练习 3**：BT 协议为什么没有 `OptsExtra`？FTP 的 `ReqExtra.TLS` 为什么放在请求侧而不是选项侧？

**答案**：BT 的「选哪些文件」由通用 `Options.SelectFiles` 承担，连接数等参数由全局 `ProtocolConfig` 管，没有任务级私有选项需求。FTP 的 TLS 模式必须在 **Resolve 阶段建立控制连接时**就生效（注释原话：resolving the link already needs a connection），而 `OptsExtra` 是创建任务时才提交的，来不及——参数该放哪一侧，取决于它在生命周期的哪个阶段被消费。

### 4.5 CreateTaskBatch 批量请求模型

#### 4.5.1 概念说明

单个任务用「`Request` + `Options`」即可描述，但真实场景常常要**一次提交一批**：JS 扩展解析出一个页面里的十几个直链、用户在 BT 资源对话框里勾选若干文件、前端「批量下载」列表。`CreateTaskBatch` 就是「三要素的批量容器」：批级 `Opts` 提供默认选项，每个成员（`CreateTaskBatchItem`）自带 `Req` 或 `Rid`，也可以用自己的 `Opts` 覆盖批级默认。

提交 1e36599（feat(tasks): group extension folder downloads）给这对结构新增了三个字段：

- `CreateTaskBatch.Group bool` 与 `GroupName string`：把整批任务放进一个「**任务分组**」（`TaskGroup`），组内任务共享一个**预留的下载目录**，任务列表里以父子关系呈现（典型场景：扩展发现的整个文件夹一次拖进来）。
- `CreateTaskBatchItem.Rid string`：成员可以引用一次 `Resolve` 返回的结果 ID，**复用已解析的 fetcher**——「先解析展示给用户、确认后再批量建任务」的两段式交互不必对每个成员重新探测。

理解这个模块的关键，是把它看作前四个模块的**组装方式**而不是新概念：成员的 `Req`/`Opts` 就是 4.1/4.3 的类型，`Rid` 指向 4.2 的 `ResolveResult`。

#### 4.5.2 核心流程

`CreateDirectBatchWithResult` 的批量规则（规格文档 [_docs/task-batch-api.md](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/_docs/task-batch-api.md) 是权威描述）：

```text
CreateTaskBatch{Reqs, Opts, Group, GroupName}
        │
        ├─ Group=true → createTaskGroup 预留 <父目录>/<组名>/
        │              （重名自动加 " (1)"、" (2)"… 后缀）
        │              成员路径被强制限制在组目录内，
        │              且 SkipAutoCategorize=true（跳过自动分类）
        ▼ 逐个成员处理（全部尝试，不因单个失败中断）
   item.Opts 存在？──是──▶ 整体替换批级 Opts（不逐字段合并）
        │ 否
        ▼ 沿用批级 Opts（两者都没有则用空 Options）
   item.Rid 非空？──是──▶ createResolved：消费 fetcherCache 里缓存的解析结果
        │ 否
   item.Req 非空？──是──▶ createDirect：从零解析并建任务
        │ 否
        ▼ 报错 "task request or rid is required"
        │
        ▼ 汇总
   全部失败   → 返回错误（并清理刚预留的空组目录与元数据）
   至少一个成功 → 返回 CreateTaskBatchResult{TaskIDs, ParentID}
```

两条最容易踩坑的规则：

1. **rid 优先**：成员同时填了 `rid` 和 `req` 时，`rid` 分支先命中，`req` 被忽略。
2. **成员 opts 是整体替换**：成员一旦带 `opts`，批级 `opts` 的所有字段（包括 path）都不再逐字段继承——想改批级默认就克隆一份完整 Options 再改。

#### 4.5.3 源码精读

批量模型定义在 [pkg/base/model.go:L202-L213](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L202-L213)，这就是穿过 REST 边界的请求体形状：

```go
type CreateTaskBatch struct {
	Reqs      []*CreateTaskBatchItem `json:"reqs"`
	Opts      *Options               `json:"opts"`
	Group     bool                   `json:"group"`
	GroupName string                 `json:"groupName"`
}

type CreateTaskBatchItem struct {
	Rid  string   `json:"rid,omitempty"`
	Req  *Request `json:"req"`
	Opts *Options `json:"opts"`
}
```

注意 `Rid` 带 `omitempty`：不引用解析结果的成员序列化后**完全不出现** `rid` 键，旧客户端提交的 `{"req": {...}}` 形状保持兼容——这是「给已有模型加字段」时的 JSON 兼容技巧。

消费入口是 [pkg/download/downloader.go:L664-L753](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L664-L753) 的 `CreateDirectBatchWithResult`。成员分发的心脏在 [pkg/download/downloader.go:L730-L741](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L730-L741)，switch 的分支顺序直接体现了「rid 优先」：

```go
switch {
case item == nil:
	itemErr = errors.New("task request or rid is required")
case item.Rid != "":
	taskID, itemErr = d.createResolved(item.Rid, opts, groupID)
case item.Req != nil:
	taskID, itemErr = d.createDirect(item.Req, opts, groupID)
default:
	itemErr = errors.New("task request or rid is required")
}
```

成员 opts 与批级 opts 的替换关系在 [pkg/download/downloader.go:L692-L705](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L692-L705)：`item.Opts` 存在就整体采用并 `Clone`，否则回落 `req.Opts`，再否则空 `Options`——没有任何逐字段合并。

分组分支在 [pkg/download/downloader.go:L674-L690](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L674-L690) 与 [pkg/download/downloader.go:L708-L729](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L708-L729)：先 `createTaskGroup` 预留目录（[pkg/download/downloader.go:L757-L826](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L757-L826)，重名探测、白名单校验、`os.Mkdir` 抢占式预留、写入 `task_group` 存储桶），随后每个成员的路径经 `filepath.Rel` 检查必须落在组目录内（`filepath.IsLocal` 拒绝 `..` 逃逸），并强制 `SkipAutoCategorize = true`。组目录预留与成员路径安全属于 u4-l6 的主题，这里只需建立印象。

部分成功语义在 [pkg/download/downloader.go:L748-L750](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L748-L750)：只有**全部**成员失败才返回错误；至少一个成功就返回结果对象，失败成员被跳过（错误用 `errors.Join` 聚合进终态错误）。

返回值模型在 [pkg/download/model.go:L40-L44](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L40-L44)，普通批量与分组批量共用：

```go
// CreateTaskBatchResult is shared by ordinary and grouped batches.
type CreateTaskBatchResult struct {
	TaskIDs  []string `json:"taskIds"`
	ParentID string   `json:"parentId,omitempty"`
}
```

`ParentID` 指向 [pkg/download/model.go:L32-L38](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L32-L38) 的 `TaskGroup`（`ID/Name/Path/CreatedAt`），组内每个任务的 `Task.ParentID` 字段（[pkg/download/model.go:L49-L54](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L49-L54)）记录这一归属。旧版只返回 `[]string` 的 Go API 由 [pkg/download/downloader.go:L654-L662](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L654-L662) 的 `CreateDirectBatch` 包装保留，内部同样委托给 `CreateDirectBatchWithResult`。

REST 侧的接线：路由注册在 [pkg/api/service.go:L55](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L55)（`POST /api/v1/tasks/batch`），handler [pkg/api/service.go:L175-L188](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L175-L188) 反序列化成 `base.CreateTaskBatch` 后直接调用 `CreateDirectBatchWithResult` 并把结果对象放进响应 `data`——**普通批量的响应也从此返回 `{"taskIds": [...], "parentId": ...}` 对象而非数组**，这是本轮 API 的一个不兼容变更（规格文档明确写了 "This replaces the former array response"）。

#### 4.5.4 代码实践

**实践目标**：亲手序列化一份「带 `Group`/`GroupName`/`Rid`」的 `CreateTaskBatch`，观察它穿过 REST 边界时的最终 JSON 形态，验证 `omitempty` 与空值字段的输出差异。

**操作步骤**：

1. 新建 `learn/u2l1/batch_learn_test.go`（示例代码）：

```go
package u2l1

import (
	"encoding/json"
	"testing"

	"github.com/GopeedLab/gopeed/pkg/base"
	download "github.com/GopeedLab/gopeed/pkg/download"
)

func TestCreateTaskBatchJSON(t *testing.T) {
	batch := &base.CreateTaskBatch{
		Group:     true,
		GroupName: "album",
		Opts:      &base.Options{Path: "/downloads"},
		Reqs: []*base.CreateTaskBatchItem{
			// 成员 1：引用一次 Resolve 的结果（rid），并自带成员级 opts
			{Rid: "V1StGXR8_Z5jdHi6B-myT", Opts: &base.Options{SelectFiles: []int{1}}},
			// 成员 2：直接给请求，不带 rid、不带成员 opts
			{Req: &base.Request{URL: "https://example.com/cover.jpg"}},
		},
	}
	data, err := json.Marshal(batch)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("CreateTaskBatch json: %s", data)

	// 对照：普通批量（Group=false）与结果对象的形态
	plain, _ := json.Marshal(&base.CreateTaskBatch{Reqs: batch.Reqs})
	t.Logf("plain batch json: %s", plain)

	result, _ := json.Marshal(&download.CreateTaskBatchResult{
		TaskIDs:  []string{"task-1", "task-2"},
		ParentID: "group-9",
	})
	t.Logf("result json: %s", result)

	emptyParent, _ := json.Marshal(&download.CreateTaskBatchResult{TaskIDs: []string{"task-1"}})
	t.Logf("ordinary result json: %s", emptyParent)
}
```

2. 运行：`go test ./learn/u2l1/ -run TestCreateTaskBatchJSON -v`

**需要观察的现象**：

- 成员 1 的 JSON 里**没有** `req` 且 `rid` 有值；成员 2 的 JSON 里**没有** `rid` 键（`omitempty` 生效）但 `opts: null` 会出现（`Opts` 无 `omitempty`）。
- `plain` 输出中 `"group":false,"groupName":""` 依然出现（`Group`/`GroupName` 无 `omitempty`，所以旧客户端发来的 JSON 反序列化后这两个字段有确定的零值）。
- `emptyParent` 输出中 `parentId` 键消失（`omitempty`），而 `taskIds` 即使为空也输出 `[]` 而非 `null`（`CreateDirectBatchWithResult` 里 `TaskIDs: make([]string, 0)` 初始化，见 [pkg/download/downloader.go:L672](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L672)）。

**预期结果**（以本地实际运行输出为准，待本地验证）。注意 `Options.Size` 带 `omitempty`，零值时 `size` 键完全不出现：

```json
{"reqs":[{"rid":"V1StGXR8_Z5jdHi6B-myT","req":null,"opts":{"name":"","path":"","selectFiles":[1],"extra":null}},{"req":{"rawUrl":"","url":"https://example.com/cover.jpg","extra":null,"labels":null,"proxy":null,"skipVerifyCert":false},"opts":null}],"opts":{"name":"","path":"/downloads","selectFiles":null,"extra":null},"group":true,"groupName":"album"}
```

```json
{"taskIds":["task-1","task-2"],"parentId":"group-9"}
```

#### 4.5.5 小练习与答案

**练习 1**：成员同时填了 `rid` 和 `req`，会发生什么？如果两者都不填呢？

**答案**：`rid` 优先——`CreateDirectBatchWithResult` 的 switch 先判断 `item.Rid != ""`，命中后走 `createResolved` 消费缓存的解析结果，`req` 被忽略（见 downloader.go:L730-L741）。两者都不填（或 `item == nil`）则该成员得到错误 "task request or rid is required"，被记入失败列表；若所有成员都这样，整个批量返回 "all batch tasks failed" 聚合错误。

**练习 2**：批级 `opts.path` 设为 `/downloads`，成员 A 不带 `opts`，成员 B 带 `opts: {"name": "x"}`（没写 path）。两个成员分别下到哪？

**答案**：成员 A 沿用批级 opts，落在 `/downloads`。成员 B 的 opts **整体替换**批级 opts（downloader.go:L698-L705 只做「存在即采用」的二元选择），`path` 为空字符串——普通批量下空路径回落到全局默认下载目录（由 `initOptions` 补齐）；分组批量下则使用预留的组目录。这正是规格文档强调 "omitted fields do not inherit individual batch values" 的原因。

**练习 3**：为什么 `CreateTaskBatchResult.TaskIDs` 要用 `make([]string, 0)` 初始化而不是留 `nil`？

**答案**：JSON 语义差异——`nil` 切片序列化成 `null`，空切片序列化成 `[]`。前端拿到 `null` 时直接 `.length` 或 `.map` 会报错，`[]` 则安全。批量 API 的消费方（Flutter 前端、MCP 客户端）不应为「零个任务成功」 special-case 空值，而且按部分成功语义，返回成功时 `TaskIDs` 至少一项，这个初始化只是兜底防御。

## 5. 综合实践

**任务：编写一份「三要素组装器」测试，把本讲五个模块串成一条链。**

在 `learn/u2l1/` 下新建 `integration_learn_test.go`，模拟一次完整的「分组批量任务提交前准备」流程（不启动网络、不创建 Downloader，纯模型层演练）：

1. **构造请求**：`Request{URL: "https://mirror.example.org/ubuntu-24.04.iso"}`，`Extra` 用 `map[string]any` 填入 `method: GET` 与两个请求头——模拟前端经 REST 提交的形态；再配一个 `RequestProxy{Mode: custom}`。
2. **转换 Extra**：`ParseReqExtra[fhttp.ReqExtra]` 把 map 变成具体类型，断言方法与头都还原成功。
3. **构造资源**：手写一个 3 文件的 `Resource`（模拟 Fetcher 解析结果），调用 `Validate`。
4. **构造选项**：`Options{Path: "/downloads", SelectFiles: []int{0, 2}, Extra: map[string]any{"connections": 8}}`，先 `Clone`，再手动做一遍 Downloader 的入口逻辑——校验下标、`InitSelectFiles`、`CalcSize`——断言最终 `Resource.Size` 等于选中两个文件之和。
5. **转换选项 Extra**：`ParseOptExtra[fhttp.OptsExtra]` 之后断言 `Connections == 8`。
6. **组装批量**：把第 1 步的请求与第 4 步的选项装进 `CreateTaskBatchItem`，再配一个只有 `Rid` 的成员，整体放进 `CreateTaskBatch{Group: true, GroupName: "ubuntu-mirror", Opts: <批级 Options>}`；把整个 batch `json.Marshal` 打印出来。
7. **打印终态**：把 `req`、`res`、`opts`、`batch` 四个对象依次 `json.Marshal` 并用 `t.Logf` 输出。

**验收标准**：

- 所有断言通过，`go test ./learn/u2l1/ -v` 全绿。
- 四段 JSON 输出与你自己手写的预期完全一致（特别是 `rawUrl` 的兜底值、`selectFiles` 的展开结果、map 键的排序、成员 2 上消失的 `rid` 键）。
- 用一句话向自己解释：为什么第 4 步里 `Clone` 必须发生在修改 `SelectFiles` 之前；再解释第 6 步里成员自带 `opts` 时批级 `opts` 的 `path` 为什么不会被子字段继承。

这个练习覆盖了本讲全部五个最小模块：Request 的边界形态（1、2）、Resource 的选择计算（3、4）、Options 的入口归一化（4）、Extra 的双向翻译（2、5）、批量模型的组装与序列化（6）。

## 6. 本讲小结

- **三要素分工**：`Request` 描述「从哪下、以什么身份下」，`Resource` 是解析阶段产出的「那里有什么」，`Options` 表达「用户想怎么下」；三者共同构成一次下载的全部输入。
- **`RawURL` 与 `URL` 分离**：原始输入与实际请求地址解耦，`ensureRequestRawURL` 保证前者永远有值；重定向、扩展改写、`Patch` 只动后者。
- **「空即全选」约定**：`Options.SelectFiles` 为空代表全选，`InitSelectFiles` 在入口处把它展开成显式下标列表，`CalcSize` 按选择覆盖式重算 `Resource.Size`——先校验、再规范化、最后重算，是 `createResolved` 入口的固定节奏。
- **`Extra any` 是边界产物**：JSON 世界里它是 `map[string]any`，Go 世界里它是各协议的 `*ReqExtra` / `*OptsExtra`；`ParseReqExtra` / `ParseOptExtra` 通过「Marshal→Unmarshal」完成翻译，幂等且写回原字段。
- **协议对照要记住不对称**：HTTP/FTP 有丰富的 `OptsExtra`（且大量字段重复但刻意不抽公共结构），BT 只有请求侧 `Trackers`，ED2K 两者皆无；参数放请求侧还是选项侧，取决于它在解析阶段还是创建阶段被消费。
- **批量模型是三要素的容器**：`CreateTaskBatch` 以批级 `Opts` + 成员 `Req`/`Rid`/`Opts` 表达一次多任务提交；`rid` 优先复用解析缓存，成员 `opts` 整体替换而非逐字段合并，全部失败才报错（部分成功）；`Group`/`GroupName` 让整批任务共享一个预留目录并以 `ParentID` 关联，响应统一为 `CreateTaskBatchResult{taskIds, parentId}` 对象。
- **三态模式贯穿始终**：代理 `follow/none/custom` 与 `*bool` 的「未设置/真/假」，都是用「可空」表达第三种语义「跟随上层配置」。

## 7. 下一步学习建议

本讲只讲了「数据长什么样」，还没讲「数据怎么流转」。下一讲 **u2-l2（Task 结构与任务状态机）** 将把 `Request + Options` 装进 `Task.Meta`、给 `Resource` 挂上状态与进度，你会看到六种任务状态（ready/wait/running/pause/error/done）如何驱动整个 UI，以及 `Task.ParentID` 如何表达本讲的分组归属。随后 **u2-l3（DownloaderStoreConfig）** 会讲同一文件下半部分的全局配置体系（`ProtocolConfig` 如何按协议名索引到本讲的各协议 Extra）。批量创建的完整运行时行为（组目录预留、去重后缀、空组清理、批量暂停/删除）则在 **u4-l6（任务分组与批量创建）** 深入展开。

想提前热身的读者可以：

- 通读 [pkg/base/model.go:L215-L234](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L215-L234) 的 `DownloaderStoreConfig` 字段注释，猜猜 `ProtocolConfig map[string]any` 的 key 是什么（答案在 u2-l3）。
- 阅读 [_docs/task-batch-api.md](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/_docs/task-batch-api.md)，对照 [pkg/download/downloader_test.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go) 中 `taskGroupBatch` 相关测试，看规格文档的每句话对应哪条测试。
- 阅读 [pkg/protocol/http/model_test.go:L20-L162](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/protocol/http/model_test.go#L20-L162)，体会项目表驱动测试对边界条件的覆盖密度。
