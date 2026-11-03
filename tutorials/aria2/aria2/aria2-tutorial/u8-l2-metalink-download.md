# Metalink 下载：从 entry 到多源任务

> 本讲属于第 8 单元「Metalink 与 RPC：服务化能力」，承接 u8-l1《Metalink 解析：基于 SAX 的状态机》的产物——一个装满 `MetalinkEntry` 的 `Metalinker`。本讲回答的问题是：**这些 entry 排好序之后，究竟是怎么变成 `RequestGroup`、又是怎么被下载的？** 另外，Metalink 还有一条完全不走 XML 文件的「隐藏通道」——Metalink/HTTP（RFC 6249），它直接从 HTTP 响应头里动态构建镜像列表，本讲一并拆解。

## 1. 本讲目标

学完本讲，你应当能够：

1. 解释 `Metalinker::queryEntry` 如何按 `--metalink-version` / `--metalink-language` / `--metalink-os` 三个条件过滤条目，以及它「消费式查询」的内存语义。
2. 说出资源优先级的方向约定（**数值越小越优先**，`999999` 为最低），并解释 `setLocationPriority` / `setProtocolPriority` 用「加减 999999」实现一票优先的技巧。
3. 跟踪 `Metalink2RequestGroup::createRequestGroup` 的完整装配流程：过滤、优先级调整、`--select-file` 裁剪、按 metaurl 分组、单文件/多文件两条装配路径、以及与 torrent 任务的 `BtDependency` 依赖注入。
4. 解释 Metalink/HTTP（RFC 6249）如何从 `Link` 头（RFC 5988）提取 `rel=duplicate` 镜像、从 `Digest` 头（RFC 3230）提取整文件校验和，并把它们动态挂到正在进行的下载上。
5. 对比 Metalink 下载与普通多 URI 下载（u3-l3 的 `createRequestGroupForUri`）在 `RequestGroup` 构造上的本质差异。

## 2. 前置知识

### 2.1 我们在哪里：从 XML 到任务的最后一段路

回顾 u8-l1 的链路：`.metalink` / `.meta4` 文件经 `MetalinkParserStateMachine`（SAX + 状态栈）解析，产出 `Metalinker`（entry 容器）与一串 `MetalinkEntry`（每个对应一个待下载文件）。再回顾 u3-l1：aria2 调度与计数的原子单位是 `RequestGroup`，它聚合 `Option`、`DownloadContext`、URI 列表与并发预算。

本讲就是两者之间的**转换器**：`Metalink2RequestGroup` 把 `MetalinkEntry` 列表翻译成 `RequestGroup` 列表。翻译过程中要做四件大事：

1. **过滤**——按用户偏好（版本/语言/操作系统）挑选 entry，丢弃 aria2 不支持的传输协议；
2. **排序**——按文档优先级 + 用户偏好（地理位置、协议）给镜像排序；
3. **装配**——把 entry 的元数据（大小、hash、分块哈希、签名）填进 `DownloadContext`；
4. **关联**——如果 entry 带有 `metaurl`（种子），额外生成一个内存中的 torrent 下载任务并建立依赖。

### 2.2 优先级的方向约定：小者优

Metalink 4（RFC 5854）中 `priority` 取值 1～999999，**1 最高、999999 最低**。aria2 内部完全沿用这个方向：

- `MetalinkResource` 构造时默认 priority 为 `getLowestPriority()` 即 999999（[src/MetalinkResource.cc:L42-L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkResource.cc#L42-L45)）；
- 排序比较器是「小于」语义：`res1->priority < res2->priority`（[src/MetalinkEntry.cc:L80-L88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkEntry.cc#L80-L88)）。

这和 u6 系列 BT 里的习惯一致：数值越小越优先。后面会看到 `--metalink-location` 与 `--metalink-preferred-protocol` 的实现都是「给匹配的资源减去 999999」，让它在任何文档优先级面前都稳赢。

### 2.3 Metalink/HTTP：没有 XML 文件的 Metalink

RFC 6249 定义了 Metalink/HTTP：服务器在**普通的 HTTP 响应**里用两个头字段描述镜像与校验和，客户端无需下载任何 `.meta4` 文件即可获得多源下载能力：

- `Link: <http://mirror/example.ext>; rel=duplicate; pri=1; geo=de`——RFC 5988 的 Link 头，`rel=duplicate` 表示「这是同一文件的另一个副本」；
- `Digest: sha-256=BASE64(...)`——RFC 3230 的 Digest 头，整文件校验和（base64 编码）。

aria2 对此是**透明处理**的：响应头一到，镜像 URI 直接追加进当前 `FileEntry` 的 URI 池，校验和写进 `DownloadContext`，后续的多连接分段下载（u3-l4）自动把它们当普通镜像用。

### 2.4 一个关键技巧：用超大数加减实现「一票优先」

想表达「这类资源必须排在最前」，最直接的做法是排序前先做一轮特殊标记，但 aria2 的实现更轻：**直接在 priority 数值上加减**。文档优先级上限是 999999，那么给偏好资源减 999999，它的优先级就变成约为负一百万的量级，必然小于任何未调整的资源。这个技巧在本讲会出现三次（location 两次、protocol 一次），认识它之后读源码会非常轻松。

### 2.5 与前置讲义的衔接

- u3-l1：`RequestGroup`、`belongsTo` / `followedBy` / `following` GID 链；
- u3-l3：任务创建流水线、`removeOneshotOption`、`--follow-metalink` 三个取值；
- u3-l4：SegmentMan 分段调度、`-s` / `-x` 的分工；
- u6-l9：magnet 的两阶段任务与 `BtDependency`——本讲的 metaurl 机制与它同构；
- u8-l1：SAX 解析器产物、`metaurl` 元素、`baseUri` 的作用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/Metalinker.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalinker.cc) | entry 容器；`queryEntry` 按 version/language/os 过滤 |
| [src/MetalinkEntry.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkEntry.cc) | 单个文件的元数据；优先级调整与资源排序的核心逻辑 |
| [src/MetalinkResource.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkResource.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkResource.cc) | 一个镜像（url、类型、location、priority）的定义 |
| [src/Metalink2RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc) | 本讲主角：entry 列表 → RequestGroup 列表的装配车间 |
| [src/metalink_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc) | `parseAndQuery` 门面与 `groupEntryByMetaurlName` 分组 |
| [src/MetalinkHttpEntry.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkHttpEntry.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkHttpEntry.cc) | Metalink/HTTP 链接条目（uri/pri/pref/geo）及其排序 |
| [src/HttpResponse.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc) | `Link` / `Digest` 响应头的解析 |
| [src/HttpResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc) | Metalink/HTTP 的消费点：把镜像挂进 FileEntry |
| [src/MetalinkPostDownloadHandler.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkPostDownloadHandler.cc) | 「透明 Metalink」：下载完 .meta4 后派生真正的任务 |
| [src/download_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc) / [src/download_handlers.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc) | 调用入口与处理器注册 |

测试参考：[test/MetalinkerTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkerTest.cc)、[test/MetalinkEntryTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkEntryTest.cc)、[test/Metalink2RequestGroupTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Metalink2RequestGroupTest.cc)、[test/MetalinkHelperTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkHelperTest.cc)，以及测试数据 [test/metalink4.xml](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/metalink4.xml)、[test/metalink4-groupbymetaurl.xml](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/metalink4-groupbymetaurl.xml)。

## 4. 核心概念与源码讲解

### 4.1 Metalinker 与 MetalinkEntry：查询过滤与资源排序

#### 4.1.1 概念说明

解析阶段结束后，`Metalinker` 只是「所有 entry 的口袋」。真正开始下载前，还有两道工序：

1. **查询（query）**：用户可用 `--metalink-version` / `--metalink-language` / `--metalink-os` 表达偏好，一份 Metalink 文档常常同时描述 Windows/Linux/macOS 多个版本、多种语言的安装包，查询就是按三个条件把匹配的 entry 挑出来。
2. **排序（reorder）**：每个 entry 挂着一串 `MetalinkResource`（镜像）。下载前要把镜像按「谁先被用」排好：文档作者用 `priority` 表达意愿，用户用 `--metalink-location`（地理位置）与 `--metalink-preferred-protocol`（协议）叠加偏好。

`MetalinkEntry` 与 `MetalinkResource` 的职责边界值得注意：**entry 是「文件」级（一个待下载文件的全部镜像 + 全部校验和），resource 是「镜像」级（一个具体的 URL）**。排序发生在 resource 层面。

#### 4.1.2 核心流程

`queryEntry` 的过滤规则（三条件同时满足才命中）：

```
对每个 entry：
  若指定了 version  且 entry->version           != version       → 跳过
  若指定了 language 且 entry 不含该 language（containsLanguage）  → 跳过
  若指定了 os       且 entry 不含该 os（containsOS）              → 跳过
  否则 → 命中，把 unique_ptr 移走（源位置变空）
最后把容器里的空指针全部清除
```

注意这是个**消费式查询**：命中的 entry 被 `std::move` 出去，`entries_` 里留下空 `unique_ptr`，随后 `remove_if` 清掉。也就是说 `Metalinker` 查一次就「空」一次，查询结果的所有权完全移交给调用方。

资源排序的完整流程（在 `Metalink2RequestGroup` 里被调用）：

```
1. dropUnsupportedResource()     丢掉 aria2 不支持的协议（见下）
2. setLocationPriority(locs, -999999)    命中 --metalink-location 的资源 priority -= 999999
3. setProtocolPriority(proto, -999999)   命中 --metalink-preferred-protocol 的资源 priority -= 999999
4. reorderResourcesByPriority()  先洗牌（shuffle）、再按 priority 升序排序
```

第 4 步「先洗牌再排序」是有意为之：`std::sort` 不保证稳定性，相同 priority 的镜像排序后顺序不确定；先做一次随机洗牌，等优先级的镜像每次运行就以随机顺序出现——这是最朴素的**同优先级负载均衡**。

#### 4.1.3 源码精读

**queryEntry**——[src/Metalinker.cc:L46-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalinker.cc#L46-L66)：三个条件任一不满足即 `continue`；命中则 `res.push_back(std::move(entry))`，最后 `remove_if` 抹掉被搬空的槽位。调用方是 [src/metalink_helper.cc:L56-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L56-L62) 的 `metalink::query`，它把三个 Pref（对应 `--metalink-version` 等选项）填进去，再由 `parseAndQuery`（[src/metalink_helper.cc:L66-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L66-L78)）串起「解析 + 查询」两步——`Metalink2RequestGroup::generate` 调用的正是这个门面。

**MetalinkResource 的定义**——[src/MetalinkResource.h:L45-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkResource.h#L45-L76)：

```cpp
enum TYPE {
  TYPE_FTP = 0, TYPE_HTTP, TYPE_HTTPS,
  TYPE_BITTORRENT, TYPE_NOT_SUPPORTED, TYPE_UNKNOWN
};
// 成员：url / type / location / priority / maxConnections
static int getLowestPriority() { return 999999; }
```

`type2String` 数组（[src/MetalinkResource.cc:L39-L40](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkResource.cc#L39-L40)）把枚举映射为 `"ftp" / "http" / "https" / "bittorrent" / "not_supported" / "unknown"` 字符串——这是 `--metalink-preferred-protocol` 选项值与内部枚举的对照表。`location` 存 ISO 3166 国家代码（如 `de`、`jp`）；`maxConnections` 是 Metalink 3 的 `maxconnections` 属性，后面装配时它会收紧 `-s` 的并发预算。

**两个优先级调整函数**——[src/MetalinkEntry.cc:L59-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkEntry.cc#L59-L78)：

```cpp
void MetalinkEntry::setLocationPriority(
    const std::vector<std::string>& locations, int priorityToAdd)
{
  for (auto& res : resources) {
    if (std::find(...) != std::end(locations)) {   // res->location 在列表中
      res->priority += priorityToAdd;              // 调用方传 -999999
    }
  }
}
// setProtocolPriority 同构：按 getTypeString(res->type) == protocol 匹配
```

对比值不是「减一个固定小数」而是减 `getLowestPriority()`（即 999999，见 4.2.3 的调用点 [src/Metalink2RequestGroup.cc:L178-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L178-L183)），效果是命中资源的新优先级约在 \(-10^6\) 量级，必然压过任何文档内优先级（最小为 1）。若 location 与 protocol 同时命中，则叠加两次减法，优先级约在 \(-2 \times 10^6\) 量级。

**排序本体**——[src/MetalinkEntry.cc:L80-L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkEntry.cc#L80-L102)：

```cpp
void MetalinkEntry::reorderResourcesByPriority()
{
  std::shuffle(std::begin(resources), std::end(resources),
               *SimpleRandomizer::getInstance());        // ① 随机洗牌
  std::sort(std::begin(resources), std::end(resources),
            PriorityHigher<std::unique_ptr<MetalinkResource>>{});  // ② 升序排
}
```

`reorderMetaurlsByPriority`（同文件 L98-L102）只做排序、不洗牌——metaurl（种子链接）数量少且语义不同，不需要负载均衡。

**丢弃不支持的资源**——[src/MetalinkEntry.cc:L126-L146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkEntry.cc#L126-L146)：`remove_if` 抹掉 `TYPE_NOT_SUPPORTED` 与 `TYPE_UNKNOWN`；`TYPE_HTTPS` 仅在编译宏 `ENABLE_SSL` 打开时保留、`TYPE_BITTORRENT` 仅在 `ENABLE_BITTORRENT` 打开时保留（呼应 u1-l2 的「configure 探测 → 编译宏 → 条件编译」链路）。顺带一个阅读彩蛋：紧挨着的仿函数类 `Supported`（[src/MetalinkEntry.cc:L104-L124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkEntry.cc#L104-L124)）在当前代码里**没有任何调用者**——真正的过滤逻辑用的是 `dropUnsupportedResource` 内的 lambda，`Supported` 是一段遗留死代码，读源码时不要被它误导。

#### 4.1.4 代码实践

本实践是本讲的主任务：**验证资源最终顺序由哪些字段决定**。好消息是无需改源码重编——`Metalink2RequestGroup` 在排序后已经打印每条资源的优先级（见 4.2.3 的 L246-L248），开 debug 日志即可观察。

1. **实践目标**：亲眼看到「文档 priority → location 偏好 → protocol 偏好 → 同级随机」四层因素如何决定镜像顺序。

2. **操作步骤**：

   1. 准备一份示例 `.meta4`（**示例代码**，命名为 `sort-test.meta4`）：

      ```xml
      <?xml version="1.0" encoding="UTF-8"?>
      <metalink xmlns="urn:ietf:params:xml:ns:metalink">
        <file name="hello.bin">
          <size>1048576</size>
          <hash type="sha-256">PLACEHOLDER</hash>
          <url location="jp" priority="5">http://mirror-jp.example/hello.bin</url>
          <url location="de" priority="5">http://mirror-de.example/hello.bin</url>
          <url location="us" priority="1">http://mirror-us.example/hello.bin</url>
          <url location="us" priority="1">https://mirror-us-secure.example/hello.bin</url>
        </file>
      </metalink>
      ```

      （hash 是占位符不影响本实验：排序发生在解析后、校验前；下载会因域名不存在而失败，但这不影响观察创建期的日志。）

   2. 基线运行：`./src/aria2c -d /tmp/ml sort-test.meta4 --log=/tmp/ml/base.log --log-level=debug`
   3. 位置偏好：`./src/aria2c -d /tmp/ml sort-test.meta4 --metalink-location=jp --log=/tmp/ml/loc.log --log-level=debug`
   4. 协议偏好：`./src/aria2c -d /tmp/ml sort-test.meta4 --metalink-preferred-protocol=https --log=/tmp/ml/proto.log --log-level=debug`
   5. 随机性检验：把基线命令连跑 5 次，对比每次日志中 `priority=5` 的两条 URL 的先后。
   6. 在日志中检索三类行：`Metalink: Queueing`（notice 级，见 [src/message.h:L133](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L133)）与 `priority=%d url=%s`（debug 级，见 [src/Metalink2RequestGroup.cc:L246-L248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L246-L248)）。

3. **需要观察的现象**：
   - 基线日志中 `priority=` 行按 1、1、5、5 的顺序出现（`mirror-us` 与 `mirror-us-secure` 在前）；
   - 加 `--metalink-location=jp` 后，`mirror-jp` 的 priority 变为 \(5 - 999999 = -999994\)，跃居第一；
   - 加 `--metalink-preferred-protocol=https` 后，`mirror-us-secure` 的 priority 变为 \(1 - 999999 = -999998\)，跃居第一；
   - 基线 5 次运行中，两条 `priority=5` 的 URL 相对顺序至少有一次不同（洗牌生效的证据）。

4. **预期结果**：上述四条全部能在日志中复核；据此写出一张「字段 → 排序影响」的推导表（文档 priority 定基调、location/protocol 各值 -999999、同级随机）。具体日志行内容以本地输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`queryEntry` 为什么要 `remove_if` 清理空指针，而不是直接保留条目？

**答案**：返回值是 `std::vector<std::unique_ptr<MetalinkEntry>>`，命中条目被 `std::move` 走后源槽位变成空 `unique_ptr`。`Metalinker` 允许多次查询（每次条件可能不同），若不清理，下一轮遍历会对空指针解引用而崩溃；清理后剩余条目仍可被后续查询命中。这是「唯一所有权 + 移动语义」下的惯用法。

**练习 2**：[test/MetalinkEntryTest.cc:L106-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MetalinkEntryTest.cc#L106-L121) 中 `setLocationPriority({"jp","al","ro"}, -100)` 之后，location 为 `ro` 的资源 priority 从 50 变成了多少？这个测试与生产代码的调用有什么参数差异？

**答案**：变为 \(-50\)（原 priority 50 加 -100）。测试传的增量是 -100，而生产代码（`Metalink2RequestGroup`）传的是 `-MetalinkResource::getLowestPriority()` 即 -999999；测试只验证「命中才加减」的机制，生产值则保证偏好压倒一切文档优先级。

**练习 3**：若一个 `.meta4` 里所有 `<url>` 的协议都是 `ftp`，而 aria2 编译时 `ENABLE_SSL` 与 `ENABLE_BITTORRENT` 都未开启，`dropUnsupportedResource` 会发生什么？

**答案**：`TYPE_FTP` 不受任何编译宏约束，资源会被完整保留（[src/MetalinkEntry.cc:L126-L146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkEntry.cc#L126-L146) 只删 `NOT_SUPPORTED` / `UNKNOWN` 及宏关闭的 HTTPS/BITTORRENT）。受宏影响的只有 https 与 torrent 两类。

### 4.2 Metalink2RequestGroup：把 entry 装配成 RequestGroup

#### 4.2.1 概念说明

`Metalink2RequestGroup` 是一个纯函数式的转换器（无状态的类，只有两个 `generate` 重载加一个 `createRequestGroup`），负责「entry 列表 → RequestGroup 列表」。它被三个入口复用：

| 入口 | 场景 | 代码位置 |
| --- | --- | --- |
| `createRequestGroupForMetalink` | `--metalink-file` 选项、RPC `aria2.addMetalink`（base64 数据经 `ByteArrayDiskWriter` 走 BinaryStream 重载） | [src/download_helper.cc:L352-L367](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L352-L367) |
| `AccRequestGroup` 仿函数 | 命令行/输入文件里的参数本身是本地 `.metalink` / `.meta4` 路径（`ProtocolDetector::guessMetalinkFile` 判定） | [src/download_helper.cc:L432-L435](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L432-L435) |
| `MetalinkPostDownloadHandler` | 「透明 Metalink」：先下载 .meta4 文件本身，再从磁盘（或内存）解析派生任务 | [src/MetalinkPostDownloadHandler.cc:L104-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkPostDownloadHandler.cc#L104-L105) |

两个 `generate` 重载分别接收**文件路径**与 **BinaryStream**（磁盘文件、内存缓冲都实现该接口，见 u5-l1 的 DiskWriter 抽象），差异只在解析入口（`parseAndQuery` 的两个版本）与 `MetadataInfo` 的构造（stdin 下载没有 gid）。

#### 4.2.2 核心流程

`createRequestGroup`（[src/Metalink2RequestGroup.cc:L151-L362](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L151-L362)）的完整流程：

```
输入：查询后的 entry 列表 + 选项模板
1. 列表为空 → 打印 "No file matched with your preference." 并返回
2. 解析 --metalink-location → locations（小写化）
3. 解析 --metalink-preferred-protocol（"none" 之外才生效）
4. 对每个 entry：
     dropUnsupportedResource()
     resources 与 metaurls 都空 → 整个 entry 跳过（无源可下）
     setLocationPriority(locations, -999999)
     有偏好协议时 setProtocolPriority(proto, -999999)
5. --select-file=INDEX...：按下标（--show-files 显示的序号）就地压缩 entry 列表
6. 每个 entry 执行 reorderMetaurlsByPriority()
7. groupEntryByMetaurlName(entries) → [(metaurl, [entries...]), ...]
8. 对每个分组：
   a. metaurl 非空：为种子 URL 建一个「迷你 RequestGroup」
      （单连接、纯内存、清理前后处理器、禁止再吃 Metalink）
   b. 建主任务 rg：
      - 组内 1 个 entry  → 单文件路径：DownloadContext(块长, 总长, 路径)
      - 组内多个 entry  → 多文件路径：逐 entry 造 FileEntry 并累加 offset
      元数据搬运：URI 列表、-x、digest、分块哈希、签名、originalName
   c. 收尾：RPC 下应用 --pause；removeOneshotOption；禁止再吃 Metalink
   d. 有种子任务时：BtDependency(rg ← torrentRg) + belongsTo +
      「全部文件都有 URI 时种子任务最多等 30 秒」
9. groups 收编全部新任务
```

第 7 步的分组规则（[src/metalink_helper.cc:L80-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L80-L109)）值得细看：函数名叫 `groupEntryByMetaurlName`，**实际分组键是第一条 metaurl 的 URL 而不是 name**；`name` 只是入组门槛——entry 的第一条 metaurl `name` 非空且 `sizeKnown` 才允许并入已有组，且已有组的首成员 name 也必须非空。语义是「多个文件共享同一个 torrent 时，合并成一个多文件 RequestGroup」，这正是 BT 多文件种子 + HTTP 镜像混合下载的形态：torrent 提供全集，HTTP 镜像为其中的单个文件加速。

#### 4.2.3 源码精读

**generate（文件路径版）**——[src/Metalink2RequestGroup.cc:L114-L134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L114-L134)：`metalink::parseAndQuery(metalinkFile, option.get(), baseUri)` 一步完成「解析 + 按偏好查询」（u8-l1 讲过 `baseUri` 用于把相对 URL 补全为绝对 URL）。随后为这批任务生成 `MetadataInfo`：本地文件路径版会创建新的 GroupId（记录「这批任务来自哪份 metalink 文件」），`DEV_STDIN`（`-`）则用无 gid 的空 `MetadataInfo`。BinaryStream 版（[L136-L149](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L136-L149)）同样以空 `MetadataInfo` 收场——透明 Metalink 场景下元数据归属由 post handler 另行设置。

**优先级调整调用点**——[src/Metalink2RequestGroup.cc:L160-L184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L160-L184)：

```cpp
entry->dropUnsupportedResource();
if (entry->resources.empty() && entry->metaurls.empty()) {
  continue;                       // 无任何可用源，整个文件放弃
}
entry->setLocationPriority(locations, -MetalinkResource::getLowestPriority());
if (!preferredProtocol.empty()) {
  entry->setProtocolPriority(preferredProtocol, -MetalinkResource::getLowestPriority());
}
```

这段就是 4.1.2 流程图里第 2～4 步的落地。注意 `--metalink-preferred-protocol` 的哨兵值 `none` 在 [L169-L172](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L169-L172) 被显式排除。

**--select-file 裁剪**——[src/Metalink2RequestGroup.cc:L185-L200](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L185-L200)：`util::parseIntSegments` 解析 `"1-3,5"` 这类区间表达式（u3-l4 见过同款），然后双指针就地压缩 `entries`，只保留下标命中的条目。这里的下标就是 `--show-files` / `-S` 打印的序号——**Metalink 的 select-file 选的是「文件条目」，而 BT 的 select-file 选的是「种子内文件」**，两者语义不同但选项同名。

**种子迷你任务**——[src/Metalink2RequestGroup.cc:L208-L236](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L208-L236)：

```cpp
createRequestGroupForUri(result, optionTemplate, uris,
                         /* ignoreForceSequential = */ true,
                         /* ignoreLocalPath = */ true);
...
torrentRg->setNumConcurrentCommand(1);
torrentRg->clearPreDownloadHandler();
torrentRg->clearPostDownloadHandler();
torrentRg->getDownloadContext()->setAcceptMetalink(false);   // 防循环
torrentRg->addPreDownloadHandler(download_handlers::getMemoryPreDownloadHandler());
torrentRg->markInMemoryDownload();
```

这与 u6-l9 magnet 的「元数据任务」完全同构：单连接、`MemoryBufferPreDownloadHandler` 让 .torrent 文件落在内存盘（`ByteArrayDiskWriter`，见 u5-l1）而不是磁盘。`setAcceptMetalink(false)` 是关键防御：如果种子的下载响应又带 Metalink/HTTP 头，不再二次解析，避免「metalink → torrent → metalink」的无限派生。

**单文件装配路径**——[src/Metalink2RequestGroup.cc:L242-L288](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L242-L288)。先排序并打日志：

```cpp
entry->reorderResourcesByPriority();
for (auto& mr : entry->resources) {
  A2_LOG_DEBUG(fmt("priority=%d url=%s", mr->priority, mr->url.c_str()));
}
```

（[L245-L248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L245-L248)，4.1.4 实践观察的就是这两行。）随后 `AccumulateNonP2PUri`（[L75-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L75-L95)）按排序后的顺序收集 HTTP/HTTPS/FTP 的 URL——**顺序即优先级**，`FileEntry` 的 URI 池靠前者优先被使用（u3-l3 的 URISelector）。几个关键赋值：

- 块长优先取分块哈希的 `pieceLength`（否则用 `-k`），保证 piece 边界与哈希边界对齐（[L254-L260](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L254-L260)）——这是 u5-l5 分块校验能工作的前提；
- `checksum` → `setDigest`、`chunkChecksum` → `setPieceHashes`、签名 → `setSignature`（[L274-L283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L274-L283)）；
- `--metalink-enable-unique-protocol` 时给 `FileEntry` 打 `setUniqueProtocol(true)`（[L271-L273](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L271-L273)）；
- 并发预算被 Metalink 3 的 `maxconnections` 收紧：`min(numSplit, entry->maxConnections)`（[L284-L287](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L284-L287)）——文档作者可以替用户压低 `-s`。

**多文件装配路径**——[src/Metalink2RequestGroup.cc:L289-L322](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L289-L322)：逐 entry 造 `FileEntry(路径, 长度, offset, uris)` 并累加 offset，offset 溢出 `int64_t` 时抛 `EX_TOO_LARGE_FILE`。这与 u3-l2 讲过的「多文件按 offset 首尾相接成一条逻辑偏移空间」完全对应——Metalink 的多文件组在存储层长得就跟 BT 多文件种子一样。每个 `FileEntry` 的 `originalName` 取自该文件自己的 `metaurls[0]->name`（种子内文件名），供 torrent 下载时对上号（[L311](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L311)）。这里能放心解引用 `metaurls[0]`，正是 4.2.2 分组门槛（name 非空且 sizeKnown）保证的。

**BtDependency 注入**——[src/Metalink2RequestGroup.cc:L333-L359](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L333-L359)：主任务 `rg` 依赖种子任务 `torrentRg`，`torrentRg->belongsTo(rg->getGID())` 挂上 u3-l1 的 GID 链。最后一段是体贴的工程处理：若**所有**文件条目都有 HTTP 镜像可用，把种子任务的 `--bt-stop-timeout` 压到最多 30 秒——「种子元数据拿不到也没关系，HTTP 镜像兜底」，避免一个失联的 tracker 拖死整个下载。

#### 4.2.4 代码实践

1. **实践目标**：观察「多个文件共享同一 metaurl」时的任务分组形态，并与单元测试互相印证。

2. **操作步骤**：
   1. 复制仓库自带的测试数据 [test/metalink4-groupbymetaurl.xml](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/metalink4-groupbymetaurl.xml)（file1、file3 共享 `http://torrent`，file2 无 metaurl）到工作目录；
   2. 用 `-S` 查看条目序号：`./src/aria2c -S metalink4-groupbymetaurl.xml`；
   3. 以 info 级日志运行：`./src/aria2c -d /tmp/ml2 metalink4-groupbymetaurl.xml --log=/tmp/ml2/run.log --log-level=info`，检索 `Processing metaurl group metaurl=`（[src/Metalink2RequestGroup.cc:L207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L207)，info 级）与 `Metalink: Queueing`；
   4. 加 `--select-file=2` 再跑一次，观察日志里只剩 file2；
   5. 阅读 [test/Metalink2RequestGroupTest.cc:L138-L207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Metalink2RequestGroupTest.cc#L138-L207) 的 `testGenerate_groupByMetaurl`：它断言 BT 开启时生成 3 个 RequestGroup——第 1 个是 `http://torrent` 的种子任务，第 2 个含 file1+file3 两个 FileEntry，第 3 个是 file2。

3. **需要观察的现象**：日志中出现两条 `Processing metaurl group`（一组键为 `http://torrent`、一组键为空）；file1 与 file3 出现在同一条 `Queueing ... as a member.`（[L296](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L296)）日志中。

4. **预期结果**：任务总数与测试断言一致（种子任务 1 + 混合组 1 + 纯 HTTP 组 1）；`--select-file=2` 后只剩 file2 一个任务。由于 URL 均为假域名，任务会在 DNS 阶段报错退出，不影响观察创建期日志（具体输出待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`groupEntryByMetaurlName` 的分组键是 metaurl 的 URL 还是 name？给出源码证据。

**答案**：是 **URL**。[src/metalink_helper.cc:L97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L97) 的比较条件是 `(*i).first == entry->metaurls[0]->url`；`name` 只出现在入组门槛里（[L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L93) 与 [L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/metalink_helper.cc#L98)）。测试数据里 file1 与 file3 的 metaurl `name` 分别是 `file1`、`file3` 却被合并，也反证了键不是 name。

**练习 2**：为什么单文件路径要 `entry->reorderResourcesByPriority()`，多文件路径却「每个 entry 各自 reorder」而不是合并排序？

**答案**：多文件组里每个文件有自己独立的镜像集合（各自的 `resources`），下载时各 FileEntry 各用各的 URI 池，不存在跨文件的统一顺序；排序的单位永远是「一个 entry 的 resources」。单文件与多文件路径都在各自 scope 内调用 `reorderResourcesByPriority`（[L245](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L245) 与 [L300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L300)）。

**练习 3**：对比普通多 URI 下载（`aria2c url1 url2 url3`）：Metalink 生成的 RequestGroup 多出了哪些信息？

**答案**：至少六项——① 文件长度（`size`，普通 URI 靠响应头才发现）；② 整文件 hash（`setDigest`）；③ 分块哈希与对齐的 piece 长度（`setPieceHashes`，支撑断点续传分块校验）；④ 签名（`setSignature`）；⑤ 每主机并发 `-x` 与被 `maxconnections` 收紧的 `-s` 预算；⑥ 可选的 torrent 依赖（`BtDependency`）与文件级 `originalName`。普通多 URI 路径（u3-l3）只有一个 URI 列表和全局选项。

### 4.3 Metalink/HTTP：从 Link 与 Digest 响应头动态构建镜像

#### 4.3.1 概念说明

Metalink/HTTP（RFC 6249）回答的问题是：**服务器已经知道镜像在哪，为什么还要让用户先下载一份 XML？** 它把镜像信息直接塞进普通 HTTP 响应头：

```
HTTP/1.1 200 OK
Link: <http://mirror1.example/hello.bin>; rel=duplicate; pri=1; geo=de
Link: <http://mirror2.example/hello.bin>; rel=duplicate; pri=9; pref
Digest: sha-256=BASE64(...)
```

`MetalinkHttpEntry` 就是这些属性的内存表示（[src/MetalinkHttpEntry.h:L46-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkHttpEntry.h#L46-L56)）：

| 字段 | 含义 | 默认值 |
| --- | --- | --- |
| `uri` | 镜像地址（`<>` 内，去除首尾空白） | 空 |
| `pri` | 优先级，1～999999，小者优 | 999999 |
| `pref` | RFC 6249 的 `pref`（IETF preference）标记，**带 pref 的无条件排最前** | false |
| `geo` | 国家代码（小写化） | 空 |

与 XML Metalink 相比它有两点先天局限：没有分块哈希（Digest 只有整文件摘要）、没有 metaurl/签名。但它是**零成本**的——用户什么都不用做。

#### 4.3.2 核心流程

```
HttpResponseCommand 收到首个响应（此时尚无 PieceStorage）：
1. 若 ctx->getAcceptMetalink() 且响应带 Link 头：
     a. setAcceptMetalink(false)            ← 一次性闸门，防重复处理
     b. getMetalinKHttpEntries() 解析所有 Link 头：
        - 仅保留 rel=duplicate 的条目
        - pri 取 1..999999，否则维持默认 999999
        - geo 小写化；pref 置位
        - --metalink-location 命中 geo 的条目 pri -= 999999
        - 排序：pref 者优先，其次 pri 升序
     c. 按序 fe->addUri(e.uri)             ← 镜像进入 FileEntry 的 URI 池
2. 若响应带 Digest 头：
     解析 "algo=base64,...";过滤不支持/长度不符的算法;
     同算法多条摘要一致才保留（冲突则整体丢弃该算法）;
     ctx 尚无 hash → 直接 setDigest;已有 → 交叉比对,不一致抛异常
```

一次性闸门 `acceptMetalink_` 的注释写得很直白（[src/DownloadContext.h:L88-L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.h#L88-L90)）：避免对同一任务把 Metalink（XML 或 HTTP）处理两遍。它的默认值在编译期决定——启用 Metalink 支持时为 true（[src/DownloadContext.cc:L57-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L57-L61)）。

#### 4.3.3 源码精读

**单条 Link 的解析**——[src/HttpResponse.cc:L283-L349](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L283-L349) 的 `parseMetalinkHttpLink`：先在整段头值里找 `<` 与 `>`，中间剥空白得到 URI；其后按 `;` 逐对解析参数（复用 u9-l1 会精读的 `util::nextParam`）：

```cpp
if (name == "rel") {
  if (value == "duplicate") { ok = true; }    // 只有 duplicate 才算镜像
  else { ok = false; }
}
if (name == "pri") {                           // 1..999999 才接受
  if (util::parseIntNoThrow(priValue, value)) {
    if (1 <= priValue && priValue <= 999999) { result.pri = priValue; }
  }
}
if (name == "geo") { util::lowercase(value); result.geo = value; }
if (value.empty() && name == "pref") { result.pref = true; }
```

返回值就是 `ok`——**没有 `rel=duplicate` 的 Link（比如样式表、alternate 语言版本）全部被拒收**，这是 RFC 6249 的硬性规定。

**排序规则**——[src/MetalinkHttpEntry.cc:L54-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkHttpEntry.cc#L54-L62)：

```cpp
bool MetalinkHttpEntry::operator<(const MetalinkHttpEntry& rhs) const
{
  if (pref ^ rhs.pref) { return pref; }   // 只有一方带 pref：带者胜
  else { return pri < rhs.pri; }          // 否则 pri 升序
}
```

**收集与 geo 加成**——[src/HttpResponse.cc:L355-L384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L355-L384)：`equalRange(LINK)` 遍历**所有** Link 头行（HttpHeader 是 multimap，允许重复头，见 u4-l2）；随后解析 `--metalink-location` 并对 geo 命中的条目 `pri -= 999999`（函数名里的大写 K 是源码原文：`getMetalinKHttpEntries`）——与 4.1 的 location 技巧完全一致；最后 `std::sort`。

**消费点**——[src/HttpResponseCommand.cc:L208-L239](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L208-L239)：

```cpp
if (!getPieceStorage()) {                    // 首个响应，文件尚未创建
  if (ctx->getAcceptMetalink()) {
    if (httpHeader->defined(HttpHeader::LINK)) {
      ctx->setAcceptMetalink(false);
      std::vector<MetalinkHttpEntry> entries;
      httpResponse->getMetalinKHttpEntries(entries, getOption());
      for (const auto& e : entries) {
        fe->addUri(e.uri);                   // 按排序后的顺序追加
        A2_LOG_DEBUG(fmt("Adding URI=%s", e.uri.c_str()));
      }
    }
  }
  if (httpHeader->defined(HttpHeader::DIGEST)) { ... }
}
```

注意 `!getPieceStorage()` 这个条件：只有「第一次拿到响应、还没建文件存储」时才吸收镜像；此后 `-s` 个连接、`-x` 每主机上限、URISelector 换源（u3-l1）等既有机制照常接管这些 URI。

**Digest 的解析与防矛盾**——[src/HttpResponse.cc:L388-L434](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L388-L434)：解析 `Digest: sha-256=..., md5=...`（摘要值 base64 解码）；用 `MessageDigest::supports` 过滤不支持的算法、比对摘要长度；按「哈希更强」排序（u5-l5 的 `HashTypeStronger`）；**同一算法出现多条且值矛盾时，该算法的所有摘要整体丢弃**（[L414-L433](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L414-L433) 的去重循环）——服务器自相矛盾的摘要不可信。消费侧在 [src/HttpResponseCommand.cc:L222-L238](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L222-L238)：任务还没有 hash 就采纳最强的那条（debug 日志 `Setting digest: type=..., digest=...`）；已有（比如来自 `--checksum`）则交叉校验，不一致直接抛 `DL_ABORT_EX("Invalid hash found in Digest header field.")`（[src/HttpResponseCommand.cc:L576-L588](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L576-L588)）。

**unique protocol 的联动**——[src/HttpResponseCommand.cc:L248-L257](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L248-L257)：若 `FileEntry` 带 `uniqueProtocol` 标志（来自 `--metalink-enable-unique-protocol`），响应到手后把与当前 URI **同主机名**的其余 URI 全部移除。同一镜像的 http/https 两条通常同主机，配合 `--metalink-preferred-protocol` 的排序，幸存者恰好是偏好的那条协议——「每个镜像只用一种协议」就这样落在 URI 池层面。

#### 4.3.4 代码实践

1. **实践目标**：用本地服务亲手发出 RFC 6249 头，观察 aria2 动态吸收镜像与摘要。

2. **操作步骤**：
   1. 准备目录 `/tmp/mh/`，生成内容文件 `hello.bin`（如 `dd if=/dev/urandom of=/tmp/mh/hello.bin bs=1k count=64`），并计算 `sha256sum hello.bin`，把十六进制摘要转 base64（Python：`base64.b64encode(bytes.fromhex(hexstr))`）；
   2. 写一个最小的镜像服务器（**示例代码**，`/tmp/mh/server.py`，端口取 8001/8002 各起一份）：

      ```python
      import http.server, sys

      class H(http.server.BaseHTTPRequestHandler):
          def do_GET(self):
              data = open('/tmp/mh/hello.bin', 'rb').read()
              self.send_response(200)
              self.send_header('Content-Length', str(len(data)))
              # RFC 6249 + RFC 5988：通告另一个镜像
              self.send_header('Link', '<http://127.0.0.1:8002/hello.bin>; rel=duplicate; pri=5; geo=us')
              # RFC 3230：整文件校验和（替换为你算出的 base64）
              self.send_header('Digest', 'sha-256=<你的base64摘要>')
              self.end_headers()
              self.wfile.write(data)

      http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
      ```

   3. `python3 /tmp/mh/server.py 8001 &` 与 `python3 /tmp/mh/server.py 8002 &`（8002 那份的 Link 头无伤大雅，可保持原样）；
   4. 运行：`./src/aria2c -d /tmp/mh/out http://127.0.0.1:8001/hello.bin -s2 -x2 --log=/tmp/mh/run.log --log-level=debug`；
   5. 检索日志中的 `Adding URI=`、`Setting digest:`；再看下载摘要输出里是否出现了到 8002 端口的连接。

3. **需要观察的现象**：日志出现 `Adding URI=http://127.0.0.1:8002/hello.bin`（[src/HttpResponseCommand.cc:L217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L217)）与 `Setting digest: type=sha-256, ...`（[L227-L228](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L227-L228)）；`-s2` 下两个连接分别打到 8001 与 8002。

4. **预期结果**：文件下载成功且校验通过；再次运行时把 Digest 值改错一个字符，aria2 应在下载完成后报校验失败（具体退出码与报错文案待本地验证，可结合 u5-l5 的 CHECKSUM_ERROR=32 对照）。

#### 4.3.5 小练习与答案

**练习 1**：`Link: <http://x/f>; rel=duplicate` 与 `Link: <http://x/f>; rel=alternate`，aria2 各会怎么处理？

**答案**：前者被 `parseMetalinkHttpLink` 接受（`rel=duplicate` 置 `ok=true`，[src/HttpResponse.cc:L322-L330](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L322-L330)）；后者 `ok=false` 被丢弃——非 duplicate 的 Link 语义不是「同内容镜像」，盲目并行下载会拿到不同文件。

**练习 2**：一个带 `pri=5` 的镜像同时命中 `--metalink-location=us`（geo=us），它排序用的最终 pri 是多少？

**答案**：\(5 - 999999 = -999994\)（[src/HttpResponse.cc:L376-L380](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L376-L380)）。若它还带 `pref`，则无论 pri 是多少都排第一（`operator<` 先比 pref）。

**练习 3**：为什么 Metalink/HTTP 无法提供分块校验（断点续传只重下坏块），而 XML Metalink 可以？

**答案**：Digest 头（RFC 3230）只能表达**整文件**摘要，没有 per-piece 哈希的编码位置；`MetalinkHttpEntry` 结构里也只有 uri/pri/pref/geo 四个字段（[src/MetalinkHttpEntry.h:L46-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkHttpEntry.h#L46-L56)）。XML Metalink 的 `<pieces>` 才携带分块哈希（4.2.3 的 `setPieceHashes`）。所以 Metalink/HTTP 的完整性保障粒度是「下完之后整体验一次」。

### 4.4 MetalinkPostDownloadHandler：透明 Metalink 的接力点

#### 4.4.1 概念说明

u3-l3 讲过 `--follow-metalink=true|mem|false`：当下载的 URI 本身指向一份 Metalink 文件（按后缀 `.metalink` / `.meta4` 或 Content-Type 判定）时，aria2 可以「顺手」解析它并派生真正的下载任务。执行这个接力的是 `MetalinkPostDownloadHandler`——一个 **post 处理器**（下载完成后触发），它把刚落盘（或落在内存）的 Metalink 文件交给 `Metalink2RequestGroup`，再把新任务挂到 GID 链上。

与之配套的 **pre 处理器**（`getMetalinkPreDownloadHandler`，[src/download_handlers.cc:L72-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc#L72-L81)）是 `MemoryBufferPreDownloadHandler`：`mem` 模式下让 Metalink 文件只进内存、不写磁盘。

#### 4.4.2 核心流程

```
RequestGroup 完成（post 处理器被调用，见 u3-l1 的 PostDownloadHandler 链）：
1. criteria 判定：单文件任务 且（路径后缀 .metalink/.meta4 或 Content-Type 匹配）
2. 用 PieceStorage 的 DiskAdaptor 以 BinaryStream 方式打开已下载的文件
   （mem 模式下数据在内存盘，同一接口）
3. 取 baseUri = 最后一个已用 URI（没有则第一个剩余 URI）→ 供相对 URL 解析
4. Metalink2RequestGroup().generate(newRgs, diskAdaptor, option, baseUri)
5. followedBy / following 双向 GID 链 + MetadataInfo
6. RPC 常驻模式下按 --pause-metadata 把新任务置为暂停
7. 新任务插回下载队列（u3-l1 的「插到等待队列头」）
```

#### 4.4.3 源码精读

**判定条件**——[src/MetalinkPostDownloadHandler.cc:L58-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkPostDownloadHandler.cc#L58-L62) 在构造函数里安装 `ContentTypeRequestGroupCriteria`，常量表来自 [src/DownloadHandlerConstants.cc:L36-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadHandlerConstants.cc#L36-L52)：后缀 `.metalink` / `.meta4`，Content-Type `application/metalink+xml` / `application/metalink4+xml`。判定实现（[src/ContentTypeRequestGroupCriteria.cc:L49-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContentTypeRequestGroupCriteria.cc#L49-L65)）有两个细节：**只对单文件任务生效**；后缀匹配不区分大小写、匹配的是「文件路径」——所以 `http://host/x.meta4` 会命中，哪怕服务器返回的 Content-Type 是 `application/octet-stream`。

**baseUri 的选取**——[src/MetalinkPostDownloadHandler.cc:L65-L88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkPostDownloadHandler.cc#L65-L88)：优先 `spentUris.back()`（真正用过的最后一个 URI，经历了重定向之后它才是「最终地址」），否则 `remainingUris.front()`。这个 URI 交给 u8-l1 的解析器当 `baseUri`，负责把 Metalink 文件里的相对 URL 补全成绝对 URL。

**派生与挂链**——[src/MetalinkPostDownloadHandler.cc:L91-L132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkPostDownloadHandler.cc#L91-L132)：`generate` 用的是 **BinaryStream 重载**（直接把 `diskAdaptor` 传进去，[L104-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkPostDownloadHandler.cc#L104-L105)）；随后 `followedBy` / `following` 建立 u3-l1 的 GID 链，`createMetadataInfoFromFirstFileEntry` 生成元数据归属；最后若在 RPC 常驻模式且开了 `--pause-metadata`，新任务一律置为暂停，等待客户端用 `aria2.unpause` 放行——这是 WebUI「添加 metalink 后先列清单再确认」体验的基础。

#### 4.4.4 代码实践

1. **实践目标**：对比 `--follow-metalink` 三个取值的行为差异，确认 post handler 的触发链。

2. **操作步骤**：
   1. 把 4.1.4 的 `sort-test.meta4`（或任何合法 .meta4）放进 `/tmp/fm/`，用 `python3 -m http.server 8000 -d /tmp/fm` 起静态服务；
   2. 三次运行（文件名故意带 `.meta4` 后缀以命中后缀判定）：
      - `./src/aria2c -d /tmp/fm/out http://127.0.0.1:8000/sort-test.meta4 --log=/tmp/fm/a.log --log-level=info`
      - 同上加 `--follow-metalink=mem`
      - 同上加 `--follow-metalink=false`
   3. 每次结束后 `ls /tmp/fm/out`，并在日志里找 `Generating RequestGroups for Metalink file`（[src/MetalinkPostDownloadHandler.cc:L95-L96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MetalinkPostDownloadHandler.cc#L95-L96)，debug 级）与 `Metalink: Queueing`。

3. **需要观察的现象**：默认（true）与 mem 都会派生子任务（日志出现 Queueing）；mem 模式下 `sort-test.meta4` 本身**不出现在**输出目录；false 模式下只有 `.meta4` 文件被保存、没有派生。

4. **预期结果**：与上一步观察一致。子任务因假域名下载失败不影响对「派生与否」的判断（具体现象待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`--follow-metalink=mem` 时 Metalink 文件存放在哪里？由哪段代码保证？

**答案**：内存（`ByteArrayDiskWriter` 一类内存盘）。由 pre 处理器 `MemoryBufferPreDownloadHandler`（[src/download_handlers.cc:L72-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc#L72-L81)）在下载开始前把任务改成 in-memory（u5-l1 讲过注入工厂改变下载目的地）；post handler 再经 `DiskAdaptor`（BinaryStream 接口）把内存里的内容喂给解析器。

**练习 2**：为什么 baseUri 取「最后一个 spent URI」而不是「用户输入的原始 URI」？

**答案**：原始 URI 可能经过 301/302 重定向（u4-l2），Metalink 文件里的相对 URL 应当相对于**最终交付内容的那台服务器**解析（RFC 3986 的相对引用解析基准是实际资源的地址）。`spentUris.back()` 正是重定向链的终点。

## 5. 综合实践

**任务：写一份「从 .meta4 到最终下载顺序」的完整推导报告。**

1. **准备数据**：自己写一份包含以下要素的 `final.meta4`——至少 3 个 `<file>` 条目（不同 version/language/os，其中 2 个共享同一个 `<metaurl>`）、每个条目 3～4 个不同 location 与协议的 `<url>`、一份 `<hash>` 与 `<pieces>`。（可参考 [test/metalink4.xml](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/metalink4.xml) 的写法。）

2. **第一轮：过滤**。用 `--metalink-version` / `--metalink-language` / `--metalink-os` 各做一次实验，用 `-S`（`--show-files`）对比条目数量变化，并对照 [src/Metalinker.cc:L46-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalinker.cc#L46-L66) 写出每个条目被保留/剔除的原因。全都不匹配时应看到 `No file matched with your preference.`（[src/message.h:L287](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L287)）。

3. **第二轮：排序**。按 4.1.4 的方法开 debug 日志，分别叠加 `--metalink-location` 与 `--metalink-preferred-protocol`，把每种组合下日志里的 `priority=` 行抄成表格，与你的手推值核对；再加一轮 `--metalink-enable-unique-protocol=true`，注意它的效果不在 priority 日志里，而要观察 URI 池中同主机 URI 的去留（见 [src/HttpResponseCommand.cc:L248-L257](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L248-L257)）。

4. **第三轮：分组与派生**。info 级日志观察 `Processing metaurl group`，确认共享 metaurl 的两个文件合并成一个多文件任务、外加一个 torrent 迷你任务（`belongsTo` GID 链可在 RPC 模式下用 `aria2.tellStatus` 的 `belongsTo` / `followedBy` 字段验证，见 u8-l4）。

5. **交付物**：一页 Markdown 报告，含（a）条目过滤决策表；（b）资源排序推导表（初始 priority → 各项加成 → 最终值）；（c）任务拓扑图（几个 RequestGroup、谁依赖谁）；（d）一句总结「Metalink 与普通多 URI 下载在 RequestGroup 构造上的差异」。

## 6. 本讲小结

- `Metalinker::queryEntry` 按 version/language/os 三条件**消费式**过滤条目；命中的 entry 被移走、空槽被清理，之后资源层再做排序。
- 优先级方向是**小者优**（默认 999999 最低）；`--metalink-location` 与 `--metalink-preferred-protocol` 的实现都是给命中资源减 999999 的一票优先技巧；`reorderResourcesByPriority` 先随机洗牌再排序，天然实现同优先级负载均衡。
- `Metalink2RequestGroup::createRequestGroup` 是装配车间：丢不支持协议 → 应用偏好 → `--select-file` 按条目下标裁剪 → 按**首条 metaurl 的 URL** 分组（name 非空且 sizeKnown 才可入组）→ 单文件/多文件两条路径装配 `DownloadContext`（块长对齐分块哈希、digest/签名/originalName 全部搬运）→ 共享种子的组额外生成一个内存 torrent 任务并注入 `BtDependency`（全部文件有镜像时种子最多等 30 秒）。
- Metalink/HTTP（RFC 6249）是零文件通道：`Link` 头里 `rel=duplicate` 的条目（uri/pri/pref/geo）排序后直接 `addUri` 进 `FileEntry` 的 URI 池；`Digest` 头（RFC 3230）提供整文件摘要，同算法摘要矛盾则整体丢弃；两者都受 `acceptMetalink_` 一次性闸门保护。
- `MetalinkPostDownloadHandler` 承接 `--follow-metalink`：按后缀或 Content-Type 判定、以 `DiskAdaptor`（BinaryStream）读取已下载文件、以最后一个 spent URI 为 baseUri 派生任务并挂 GID 链；`mem` 模式由 pre 处理器让 Metalink 文件全程留在内存。

## 7. 下一步学习建议

本讲之后，第 8 单元还剩服务化两条线：

1. **u8-l3 内嵌 HTTP 与 WebSocket 服务器**：`--enable-rpc` 之后 `HttpListenCommand` / `HttpServer` 如何接受连接、解析请求——你会看到本讲的 `HttpHeaderProcessor`（u4-l2）在服务端方向被复用。
2. **u8-l4 RPC 方法分发**：`aria2.addMetalink` 正是通过本讲的 `createRequestGroupForMetalink`（[src/download_helper.cc:L352-L367](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L352-L367) 所在调用链）注入任务的，学 RPC 时可以回来对照。

如果想在源码上继续深挖，建议阅读：`RequestGroup::followedBy` / `belongsTo` 的实现（GID 链的数据结构），以及 `MemoryBufferPreDownloadHandler` 如何改写 PieceStorage 的落盘目的地（与 u5-l1 的 DiskWriterFactory 体系衔接）。
