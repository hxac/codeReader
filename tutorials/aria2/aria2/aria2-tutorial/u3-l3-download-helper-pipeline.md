# 任务创建流水线：download_helper 与协议识别

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出从「命令行上的 argv」到「引擎里可执行的 `RequestGroup` 集合」的完整流水线，以及 `Context` 构造函数中 torrent / metalink / input-file / URI 四个互斥分支的判断顺序。
2. 精读 `UriListParser`，写出 `-i` 输入文件的完整语法（URI 行、缩进选项行、注释行），并解释「只有部分选项允许在输入文件里按条目覆盖」的过滤机制。
3. 精读 `createRequestGroupForUri` 的三步：参数化 URI 展开（`paramed_string::expand`）、`stable_partition` 协议分组、`splitURI` 的 URI 复制算法，并能手工预测「给定 URI 列表与 `-s`/`-x` 值，最终生成几个 RequestGroup、每个携带几条 URI」。
4. 解释 `DownloadHandler`（pre/post）抽象如何让「下载一个东西」派生出「再下载另一些东西」（Metalink→文件、torrent→数据、magnet→metadata）。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先简单回顾：

- **RequestGroup（u3-l1）**：aria2 对「一次下载任务」的容器抽象。本讲要回答的问题正是「这些 RequestGroup 是谁、在哪里、按什么规则被造出来的」。
- **Option / OptionParser / OptionHandler（u1-l5）**：所有选项都登记在 `OptionParser` 的注册表里，值的校验统一走 `OptionHandler::parse`。本讲会看到输入文件里的 `dir=/tmp` 这类文本复用同一条校验通道。
- **DownloadEngine（u2-l1）**：引擎只消费现成的 RequestGroup 集合，不关心它们怎么来的。本讲是引擎启动**之前**的最后一段装配线。
- **一次性选项（oneshot option）**：像 `--gid`、`--pause` 这类只应作用于「紧接着那一个任务」的选项，任务创建后必须从选项模板里清除，否则会污染后续派生任务。
- **stable_partition**：C++ 标准库算法，把满足谓词的元素挪到前半段、不满足的挪到后半段，**两段内部保持原有相对顺序**。本讲用它做「流协议 vs 本地文件」的分组。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/Context.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc) | 启动总装：按 torrent/metalink/input-file/URI 四分支调用 download_helper |
| [src/download_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc) | 本讲主角：四个 `createRequestGroupFor*` 工厂 + 协议识别分组 |
| [src/UriListParser.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UriListParser.cc) | `-i` 输入文件的行级解析器 |
| [src/paramed_string.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/paramed_string.h) | 参数化 URI 展开（`{}` 选择、`[]` 循环），模板函数全部在头文件 |
| [src/ProtocolDetector.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ProtocolDetector.cc) | 「嗅探」一个字符串是流协议 URI、magnet、种子文件还是 Metalink 文件 |
| [src/download_handlers.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc) | pre/post 下载处理器的进程级单例工厂 |
| [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc) | 处理器的挂接（构造时）与触发（pre/postDownloadProcessing） |
| [src/ContentTypeRequestGroupCriteria.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContentTypeRequestGroupCriteria.cc) | 「能否处理」的判定：按文件后缀或 Content-Type 匹配 |
| [src/MultiUrlRequestInfo.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc) | 接过 RequestGroup 集合交给 DownloadEngineFactory |
| [test/DownloadHelperTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadHelperTest.cc) / [test/UriListParserTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UriListParserTest.cc) | 官方测试，是本讲多个实践任务的「标准答案」 |

## 4. 核心概念与源码讲解

### 4.1 流水线总览：四个入口分支，六处调用方

#### 4.1.1 概念说明

aria2 有六种方式可以「凭空造出下载任务」：命令行 URI、`--torrent-file`、`--metalink-file`、`-i` 输入文件、RPC 调用（`aria2.addUri` 等）、libaria2 的 C API。如果六处各自写一套解析逻辑，协议识别、选项继承、GID 分配的规则就会分叉。

`download_helper.cc` 的设计是把这些入口**收敛到四个工厂函数**：

| 工厂函数 | 输入 | 输出 |
| --- | --- | --- |
| `createRequestGroupForBitTorrent`（两个重载） | torrent 文件路径（或已解码的 `ValueBase`） | 1 个 BT 任务 |
| `createRequestGroupForMetalink` | Metalink 文件路径（或其文本内容） | N 个任务（每个 entry 一个） |
| `createRequestGroupForUriList` | `-i` 文件 | N 个任务（每个条目至少一个） |
| `createRequestGroupForUri` | 一串 URI/路径 | 1..N 个任务（见 4.3） |

命令行模式下的「选哪一个工厂」由 `Context` 构造函数决定，判断顺序是**互斥的四级 if-else 链**。

#### 4.1.2 核心流程

```
argv
 │ (u1-l3/u1-l5：选项解析之后)
 ▼
Context 构造函数
 ├─ ① --torrent-file 非空?  ──是──▶ createRequestGroupForBitTorrent
 ├─ ② --metalink-file 非空? ──是──▶ createRequestGroupForMetalink
 ├─ ③ --input-file 非空?    ──是──▶ --deferred-input?
 │                                  ├─ 是：只 openUriListParser，暂不解析
 │                                  └─ 否：createRequestGroupForUriList
 └─ ④ 否则                    ─────▶ createRequestGroupForUri(命令行剩余参数)
 │
 ▼ 清理一次性选项（--out/--force-sequential/--input-file/--index-out/
 │   --select-file/--pause/--checksum/--gid）
 ▼
 requestGroups ──▶ MultiUrlRequestInfo ──▶ prepare(): DownloadEngineFactory
                                              .newDownloadEngine(option, groups)
```

注意 ①②③④ 是 if/else 链：给了 `--torrent-file` 就**只**下载种子，命令行上的 URI 会被当作 BT 的辅助 web-seed URI（传给 `createRequestGroupForBitTorrent` 的 `uris` 参数），而不是另开 HTTP 任务。

#### 4.1.3 源码精读

Context 构造函数尾部就是这条四分支链（`#ifdef ENABLE_BITTORRENT` / `ENABLE_METALINK` 包裹前两级，所以裁剪编译后分支会变短）：

- [src/Context.cc:L246-L289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L246-L289)：依次检查 `PREF_TORRENT_FILE` → `PREF_METALINK_FILE` → `PREF_INPUT_FILE` → 兜底把 `args`（非选项参数，即 URI 列表）交给 `createRequestGroupForUri`。前两级里还嵌着 `--show-files` 的提前退出（只列文件不解码下载）。第三级里若 `--deferred-input=true`，只 `openUriListParser` 保存起来，等 RPC/引擎运行期再逐条解析。
- [src/Context.cc:L291-L302](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L291-L302)：任务造完后立刻从全局选项模板里 `remove` 八个「命令行专用」选项——因为这份 `op` 之后还会作为**模板**派生新任务（如 RPC addUri），不清除会导致后续任务被意外套上 `-o`、`-i` 等设置。这与 4.3.3 里 `removeOneshotOption` 是同一个思想的两层实现。
- [src/Context.cc:L309-L314](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L309-L314)：`requestGroups` 被移动进 `MultiUrlRequestInfo`。日志 `"Downloading N item(s)"` 中的 N 就是 `requestGroups.size()`——这是本讲所有实践任务「验证生成了几个任务」的观测点。
- [src/MultiUrlRequestInfo.cc:L205-L207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L205-L207)：`prepare()` 里把整个集合移交给 `DownloadEngineFactory().newDownloadEngine`。流水线到此交接给 u2-l1 讲过的引擎循环。

「一处工厂、多处调用」的完整盘点（用 Grep `createRequestGroupFor` 即可复现）：

| 调用方 | 位置 | 特点 |
| --- | --- | --- |
| 命令行（Context） | [src/Context.cc:L255-L288](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L255-L288) | 本讲主线，`throwOnError=true` |
| libaria2 `addUri` | [src/aria2api.cc:L279-L281](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L279-L281) | `ignoreForceSequential=true, ignoreLocalPath=true`——库模式不接受本地种子/Metalink 路径 |
| RPC `aria2.addUri` 等 | [src/RpcMethodImpl.cc:L248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L248) | 与库模式同样收敛 |
| Metalink 派生 | [src/Metalink2RequestGroup.cc:L215-L231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L215-L231) | 每个 Metalink entry 的资源列表转成一个流协议任务 |
| BT 元数据派生 | [src/BtPostDownloadHandler.cc:L103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPostDownloadHandler.cc#L103)、[src/UTMetadataPostDownloadHandler.cc:L103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataPostDownloadHandler.cc#L103) | magnet 先下 metadata，收齐后**用同一工厂**再造数据任务（u6-l9） |

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「N 个 URI 在默认模式下只生成 1 个 RequestGroup，而 `-Z` 下生成 N 个」。
2. **操作步骤**：
   - `./src/aria2c -l log.txt --dry-run http://example.com/a http://example.com/b http://example.com/c`（`--dry-run` 让任务创建但不真正传输；若本机构建不支持网络访问，任选可访问的两个 URL，或标记待本地验证）。
   - 再执行 `./src/aria2c -Z -l log2.txt --dry-run <同样三个 URI>`。
   - 在两份日志里各搜一次 `Downloading`。
3. **需要观察的现象**：第一次日志出现 `Downloading 1 item(s)`，第二次出现 `Downloading 3 item(s)`。
4. **预期结果**：与 [src/Context.cc:L310-L311](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L310-L311) 的 `requestGroups.size()` 输出一致；差异原因在 4.3.2 的「`-Z` 分支」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Context` 里清理的选项名单（8 个）比 `removeOneshotOption`（2 个）长得多？
**答案**：`removeOneshotOption`（[src/download_helper.cc:L596-L600](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L596-L600)）在每个 RequestGroup 的选项副本上执行，只需移除会影响「下一个同批任务」的 `--gid`/`--pause`；而 Context 清理的是**进程级模板**，所有「语义上只针对本次命令行输入」的选项（`-o`、`-i`、`--select-file` 等）都必须清掉，否则会泄漏到之后由 RPC 派生的任务里。见 [src/Context.cc:L291-L294](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L291-L294) 的注释。

**练习 2**：`--deferred-input` 存在的意义是什么？
**答案**：不加它时 `-i` 文件在启动时被一次性解析成任务塞满等待队列；加了之后只创建 `UriListParser`（[src/Context.cc:L273-L276](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L273-L276)），配合 `--enable-rpc` 在运行期按需读取，文件里后续新增的条目也能被发现。

**练习 3**：`--torrent-file=x.torrent http://mirror/file` 里的 URI 会变成什么？
**答案**：走分支 ①，URI 列表作为 `uris` 参数传入 `createRequestGroupForBitTorrent`（[src/Context.cc:L255-L256](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L255-L256)），最终成为该种子的 web-seed URI（HTTP 做种，见 u6-l1），不会生成独立的 HTTP 任务。

### 4.2 UriListParser：`-i` 输入文件的行级解析

#### 4.2.1 概念说明

`-i` 让用户把大量任务写进一个文本文件。这个文件的语法极简，但有几个容易踩的坑（URI 之间必须用 **Tab** 分隔、选项行必须**缩进**）。解析器 `UriListParser` 是一个「带 lookahead 的逐条读取器」：每次 `parseNext` 吐出**一个条目**——一组 URI 加一个临时 `Option`。

#### 4.2.2 核心流程

输入文件语法（以官方测试夹具为准，[test/filelist1.txt:L1-L8](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/filelist1.txt#L1-L8)）：

```
# comment line                          ← 首字符 '#' 的行是注释，跳过
http://localhost/index.html	http://localhost2/index.html
                                        ← 空行分隔条目
ftp://localhost/aria2.tar.bz2           ← URI 行：一个或多个 URI，用 Tab 分隔
  dir=/tmp                              ← 选项行：以空格或 Tab 开头，name=value
# comment line
	 out=chunky_chocolate               ← 缩进的 Tab 也行，多个选项行累积
```

解析状态机（`parseNext` 的两层循环）：

```
外层循环：找「下一个 URI 行」
  line_ 为空 → getLine() 取下一行
  空行/注释行 → 继续取
  撞到非注释非空行 → 这就是 URI 行：
      ① 按 '\t' 切分成 uris（允许多 URI 指向同一文件）
      ② 进入内层循环收集选项行：
           行首是空格/Tab → 追加进 stringstream（补 '\n'）
           '#' 开头 → 跳过
           空行 → 继续读（EOF 则结束）
           其他（新的 URI 行）→ break，该行**留在 line_ 里**
      ③ optparser->parse(op, ss) 解析选项文本
      ④ return —— 下次 parseNext 从 line_ 里那条 URI 行继续
```

关键点：`line_` 成员就是「预读一行」的回退机制（pushback），保证新 URI 行不会被吃掉。

#### 4.2.3 源码精读

- [src/UriListParser.cc:L54-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UriListParser.cc#L54-L61)：构造函数按 `HAVE_ZLIB` 决定用 `GZipFile` 还是 `BufferedFile` 打开文件——**输入文件可以是 gzip 压缩的**，扩展名不需要是 `.gz`。
- [src/UriListParser.cc:L65-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UriListParser.cc#L65-L99)：`parseNext` 主体。L69 判断当前 `line_` 是否 URI 行；L70-71 用 `util::split(..., '\t', true)` 按 Tab 切分（最后一个参数 `true` 表示丢弃空 token，所以行尾多余 Tab 无害）；L73-96 内层循环把缩进行攒进 `ss`；L97 调 `optparser->parse`。
- [src/UriListParser.cc:L100-L108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UriListParser.cc#L100-L108)：取行与 EOF/I-O 错误处理——EOF 时静默返回（`uris` 为空表），读失败抛 `DL_ABORT_EX("UriListParser:I/O error.")`。
- [src/UriListParser.cc:L112-L119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UriListParser.cc#L112-L119)：`hasNext` = 「还有预读行」或「文件未到 EOF」；返回 false 时顺手 `close()`。
- [src/OptionParser.cc:L237-L256](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L237-L256)：选项文本的真正解析：逐行 `name=value`，经 `option::k2p` 把名字换成 Pref，再走 `handler->parse`——**与命令行、配置文件共用同一套校验**（u1-l5）；未知选项只 WARN 不中断。
- [src/download_helper.cc:L522-L554](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L522-L554)：`createRequestGroupFromUriListParser`——把「一个条目」变成任务，并做两层过滤：
  - L538 `requestOption->remove(PREF_OUT)`：先剥掉命令行上的全局 `-o`，否则文件里每个任务都会写到同一个文件名。
  - L540-546 遍历**全部**已注册选项，仅当 `h->getInitialOption() && tempOption.defined(pref)` 时才把条目级值拷入——`setInitialOption(true)` 在 `OptionHandlerFactory` 里逐个标注（[src/OptionHandlerFactory.cc:L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L60) 等 130+ 处），这就是「输入文件里只能覆盖白名单选项」的机制。`dir`、`out` 在白名单里，而 `--torrent-file` 这类入口级选项不在。
  - L548-551：`createRequestGroupForUri(..., throwOnError=false)`——坏条目只记日志，解析继续；一旦本条目产出了任务就 `return true`。
- [src/download_helper.cc:L556-L578](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L556-L578)：`openUriListParser` 做存在性检查（不存在或是目录则抛 `EX_FILE_OPEN`）；`createRequestGroupForUriList` 用 `while(createRequestGroupFromUriListParser(...))` 反复调用直到输入耗尽——所以整份文件是**一条一条**（而不是一次全量）转成任务的。
- 顺带一提 `-i -`（从标准输入读）：`-` 并没有在 UriListParser 里特判，而是在选项解析层被 [src/OptionHandlerImpl.cc:L547-L549](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc#L547-L549) 的 `LocalFilePathOptionHandler` 翻译成 `/dev/stdin`。

#### 4.2.4 代码实践（本讲指定实践任务）

1. **实践目标**：验证输入文件「一个条目 = 一组 URI + 一份选项覆盖 = 一个（或多个）RequestGroup」，并逐行解释解析规则。
2. **操作步骤**：
   1. 新建 `uris.txt`（注意 URI 行内是 **Tab**、选项行行首是空格）：

      ```
      http://example.com/file1	http://mirror.example.com/file1

      http://example.com/file2
        out=saved2.tar.gz
        dir=/tmp/aria2-practice
      ```

   2. 运行 `./src/aria2c -i uris.txt -l log.txt --dry-run`（无 `--dry-run` 也可，但会真实下载，视网络情况标记待本地验证）。
   3. 打开 `log.txt` 搜 `Downloading`，确认是 `2 item(s)`；再搜 `saved2`、`/tmp/aria2-practice`，确认第二条目把文件名与目录都改写了。
3. **需要观察的现象**：日志显示 2 个任务；第二个任务的目标路径是 `/tmp/aria2-practice/saved2.tar.gz`；第一条目的两个 URI 属于同一个任务（多镜像），没有生成第 3 个任务。
4. **预期结果**：条目一 → 1 个 RequestGroup（FileEntry 挂 2 条 URI，经 `splitURI` 可能复制成多份，见 4.3）；条目二 → 1 个 RequestGroup 且 `out=`/`dir=` 生效；两行之间必须有空行或其他条目分隔，否则第二条会被当成第一条的 URI 行合并（Tab 分隔时）。对照 [src/download_helper.cc:L530-L548](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L530-L548)：每个条目独立走一次 `createRequestGroupForUri`。
5. 另一个零成本的对照：官方测试 [test/UriListParserTest.cc:L42-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UriListParserTest.cc#L42-L78) 断言了 `filelist1.txt` 解析出 2 个条目、第二条目带 `dir=/tmp` 与 `out=chunky_chocolate`——你的实验结果应与之同构。

#### 4.2.5 小练习与答案

**练习 1**：若两个条目之间没有空行，第二行的 URI 会怎样？
**答案**：`parseNext` 的内层循环（[src/UriListParser.cc:L87-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UriListParser.cc#L87-L95)）遇到「行首不是空格/Tab 也不是 #」就 break，该行留在 `line_` 成为下一批 URI 行——但如果它以 Tab 与前一行写在**同一行**上，则会被当作同一文件的多镜像合并进 `uris`。
**练习 2**：在输入文件里写 `torrent-file=xx.torrent` 会怎样？
**答案**：`OptionParser::parse` 只 WARN「Unknown option」或（若该选项存在但未标 initial）在 [src/download_helper.cc:L540-L546](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L540-L546) 的白名单过滤中被丢弃——条目级覆盖只对 initial option 生效。
**练习 3**：为什么 `createRequestGroupFromUriListParser` 每次只处理到「第一个成功产出任务的条目」就返回？
**答案**：见 L547-551 的注释与代码：坏条目（解析失败/未识别 URI）在该函数内部已被吞掉，函数用 `result.size()` 是否增长来上报「有无进展」；外层 `while` 循环靠这个布尔值逐条推进，天然实现「跳过坏条目、继续读文件」。

### 4.3 `createRequestGroupForUri`：协议识别、分组与参数化展开

#### 4.3.1 概念说明

这是流水线上最「聪明」的一个函数：给它一串杂乱的字符串（HTTP URL、magnet、本地 `.torrent` 路径、`.metalink` 路径混在一起），它要决定**谁和谁合并进同一个任务、谁单开任务、每个任务复制几份 URI**。它分三步：

1. **展开**：`-P` 开启时先把参数化 URI 展开成多个具体 URI；
2. **分组**：`-Z` 逐 URI 建任务；默认则把「流协议 URI」合并为一个多镜像任务，其余逐个处理；
3. **识别**：非流协议的每个字符串交给 `ProtocolDetector` 嗅探类型。

#### 4.3.2 核心流程

```
createRequestGroupForUri(result, option, uris, ...)
 │
 ├─ ① -P? ──是──▶ unfoldURI: 对每个 uri 调 paramed_string::expand
 │                 "http://srv{1,2}/f"  → "http://srv1/f","http://srv2/f"
 │                 "http://srv[1-3:2]/f" → srv1, srv3
 │
 ├─ ② --force-sequential (-Z)? ──是──▶ 对每个 URI 独立执行 AccRequestGroup
 │                                       （流协议 URI 也一 URI 一任务）
 │
 └─ ② 否 ──▶ stable_partition：流协议 URI 排前半，保持相对顺序
      ├─ 前半非空：splitURI 复制补齐到约 numSplit 份
      │    └─ createRequestGroup(option, streamURIs, useOutOption=true)
      │         + setNumConcurrentCommand(numSplit)   ← 1 个任务
      └─ 后半逐个：AccRequestGroup
            ├─ isStreamProtocol?        →（此分支到不了，已在前面）
            ├─ guessTorrentMagnet?      → createBtMagnetRequestGroup
            ├─ guessTorrentFile?        → 读文件 bencode 解析 → createBtRequestGroup
            ├─ guessMetalinkFile?       → Metalink2RequestGroup().generate
            └─ 都不是                   → "unrecognized URI" 日志或抛异常
```

`splitURI` 的复制算法（设 URI 数为 \(n\)，目标份数 `numSplit` \(s\)，上限 `maxIter` \(m\)，且 \(n < s\)）：

\[ \text{重复整列表} \; \min(\lfloor s/n \rfloor, m) \; \text{次，若重复次数} < m \text{ 再补前} \; s \bmod n \; \text{个} \]

例：\(n=3, s=7, m=2\) → 整列表重复 \(\min(2,2)=2\) 次得 6 份，补齐条件不满足 → 6 份；\(n=3, s=5, m=2\) → 重复 1 次得 3 份，\(1<2\) 再补前 \(5\bmod 3=2\) 个 → 5 份。这些数字正是官方测试的断言值。

#### 4.3.3 源码精读

**第一步：参数化展开**

- [src/download_helper.cc:L75-L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L75-L83)：`unfoldURI` 逐条调用 `paramed_string::expand`。
- [src/paramed_string.h:L222-L248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/paramed_string.h#L222-L248)：`expand` 主循环——从 `res={""}` 出发，扫描到 `{` 或 `[` 前的普通文本追加到**所有**已有组合上，再调 `expandChoice`/`expandLoop` 做笛卡尔积式扩张（`res2.reserve(res.size() * choices.size()` 可见是乘法增长）。头部注释（[L186-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/paramed_string.h#L186-L221)）就是官方语法文档：`[START-END:STEP]` 与 `{C1,C2,...}`，两端闭区间，数值上限 65535。
- [src/paramed_string.h:L52-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/paramed_string.h#L52-L77)：`expandChoice`——找到配对的 `}`（缺失则抛 `Missing '}'`），按 `,` 切分选项，对 `res` 中每个串分别追加每个选项。
- [src/paramed_string.h:L95-L184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/paramed_string.h#L95-L184)：`expandLoop`——支持三种 range：纯数字、纯小写字母、纯大写字母（字母按 26 进制，[src/paramed_string.cc:L41-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/paramed_string.cc#L41-L56) 的 `toBase26` 与反向的 `fromBase26`）；`:` 后是步长；**START 与 END 等长时保留前导零**（L131-137 用 `%0<width>d` 格式化），所以 `[05-10:5]` 展开为 `05`、`10` 而非 `5`、`10`。

**第二步：`-Z` 与分组**

- [src/download_helper.cc:L479-L491](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L479-L491)：先展开，再看 `PREF_FORCE_SEQUENTIAL`——`-Z` 时对每个 URI 独立跑 `AccRequestGroup`，**没有合并**。
- [src/download_helper.cc:L492-L519](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L492-L519)：默认分支。`std::stable_partition` 以 `StreamProtocolFilter`（[L461-L472](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L461-L472)，仅包一层 `isStreamProtocol`）为谓词把流协议排前；注释「let's process http/ftp protocols first」说明**先**建流协议任务、**再**处理本地种子/Metalink。前半交给 `splitURI`（[L86-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L86-L104)，算法见上）后 `createRequestGroup(option, streamURIs, true)` 生成**唯一**一个多镜像任务。
- [src/download_helper.cc:L86-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L86-L104)：注意第一个 if——`numURIs >= numSplit` 时**全部原样插入、不截断**（URI 只会多不会少）。

**第三步：协议嗅探（AccRequestGroup + ProtocolDetector）**

- [src/download_helper.cc:L390-L457](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L390-L457)：`AccRequestGroup::operator()` 的四级判断顺序：`isStreamProtocol` → `guessTorrentMagnet` → `guessTorrentFile` → `guessMetalinkFile` → 兜底「unrecognized URI」。流协议分支（L392-403）把同一 URI 复制 \(\min(x,s)\) 份再建任务——这就是 `-Z` 下单 URI 任务的形态。种子/Metalink 解析失败时按 `throwOnError_` 决定抛出还是仅 `A2_LOG_ERROR_EX`（RPC 路径 `throwOnError=true`）。
- [src/ProtocolDetector.cc:L56-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ProtocolDetector.cc#L56-L59)：`isStreamProtocol` 就是 `uri_split(...) == 0`。而 `uri_split` 的状态机要求 scheme 后**必须紧跟 `//`**（[src/uri_split.c:L98-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri_split.c#L98-L113)，否则立即返回 -1）。所以这个「流协议判定」不是白名单，而是「形如 `scheme://...` 即可」——`magnet:?xt=...` 因缺 `//` 落不进流协议，才能被后面的 magnet 分支接住。
- [src/ProtocolDetector.cc:L78-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ProtocolDetector.cc#L78-L91)：magnet 判定直接尝试 `bittorrent::parseMagnet`，成功即为真。
- [src/ProtocolDetector.cc:L61-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ProtocolDetector.cc#L61-L76) 与 [L93-L108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ProtocolDetector.cc#L93-L108)：本地文件靠**魔数**而不是扩展名——种子读首字节 `'d'`（bencode 字典开头），Metalink 读前 5 字节 `<?xml`。所以文件改名 `.torrent` 骗不过它，扩展名只在 4.4 的 criteria 里起作用。

**第四步：任务对象装配**

- [src/download_helper.cc:L129-L163](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L129-L163)：`createRequestGroup` 是流协议任务的装配线：`util::copy(option)` 克隆选项（每个任务独立副本）；`getGID`（[L107-L126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L107-L126)）处理 `--gid`：显式给值则校验格式与唯一性，否则 `GroupId::create()` 随机生成；`DownloadContext` 以 `--piece-length` 与总长 0 创建，`useOutOption && -o` 非空时把输出路径定为 `dir + out`，否则路径为空（等 HTTP 响应或 Content-Disposition 决定文件名，u4-l1）；`--checksum=type=hex` 在此拆开写入 dctx（L145-153）；最后 `removeOneshotOption`。
- [src/download_helper.cc:L184-L232](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L184-L232)：`createBtRequestGroup`——`bittorrent::loadFromMemory` 把 bencode 字典灌进 DownloadContext（u6-l1 展开），web-seed URI 洗牌，`--select-file`/`--index-out` 在此生效，并 `setAcceptMetalink(false)` 防止 web-seed 请求被服务器用 Metalink 应答。
- [src/download_helper.cc:L235-L302](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L235-L302)：`createBtMagnetRequestGroup`——magnet 只知 infohash，于是建一个「总长未知 + 内存盘 + 只挂 UTMetadataPostDownloadHandler」的特殊任务（u6-l9 展开）；若 `--bt-load-saved-metadata` 命中本地缓存种子还会直接跳过 metadata 阶段（L248-274）。
- [src/download_helper.cc:L351-L367](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L351-L367)：`createRequestGroupForMetalink` 只是 `Metalink2RequestGroup().generate` 的包装，支持传文件路径或内存字符串（RPC 用）。

#### 4.3.4 代码实践

1. **实践目标**：用官方测试当「可执行的标准答案」，先手工预测 `splitURI` 与参数化展开的结果，再跑测试核对。
2. **操作步骤**：
   1. 读 [test/DownloadHelperTest.cc:L67-L132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadHelperTest.cc#L67-L132)：3 个 URI、`-s7 -x2` → 断言 1 个任务、6 条 URI、`numConcurrentCommand=7`、路径 `/tmp/file.out`。
   2. 用 4.3.2 的公式自己算一遍：\(n=3, s=7, m=2\) → 6；\(n=3, s=5, m=2\) → 5；\(n=3, s=2\) → \(n \ge s\) 全量 3。
   3. 编译并只跑该用例：`make check` 全量较慢，可先 `test/aria2_tests DownloadHelperTest`（具体目标名以 `test/Makefile.am` 为准，待本地验证）。
   4. 参数化实验：`./src/aria2c -P --dry-run -l log.txt 'http://srv[1-4:2]/file' 'http://host/{alpha,bravo}/f'`，然后到日志里数任务与 URI——预期展开为 `srv1`、`srv3` 与 `alpha`、`bravo` 共 4 条 URI、1 个任务（同组流协议合并）。
3. **需要观察的现象**：步骤 4 的日志 `Downloading 1 item(s)`；`-P` 与否的差别（不加 `-P` 时 `[1-4:2]` 原样当 URI，因不含 `//` 前的合法 scheme 而被报 unrecognized URI）。
4. **预期结果**：手工计算与测试断言一致；参数化展开结果与 [test/DownloadHelperTest.cc:L134-L161](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadHelperTest.cc#L134-L161) 的 `testCreateRequestGroupForUri_parameterized`（`http://{alpha, bravo}/file` → 两条）行为一致。

#### 4.3.5 小练习与答案

**练习 1**：`aria2c -Z a.torrent http://h/f` 会生成几个任务？顺序如何？
**答案**：2 个。`-Z` 路径对每个 URI 顺序执行 AccRequestGroup：`a.torrent` 产出 BT 任务，`http://h/f` 产出流协议任务（URI 复制 \(\min(x,s)\) 份）。若不加 `-Z`，则 `stable_partition` 先建 1 个 HTTP 任务、再建 1 个 BT 任务——任务顺序不同、数量相同。对照 [test/DownloadHelperTest.cc:L164-L199](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadHelperTest.cc#L164-L199)：混排时 HTTP 任务在前（`result[0]`）、种子任务在后（`result[1]`）。
**练习 2**：为什么 `isStreamProtocol` 不维护一个 `http/https/ftp/sftp` 的协议白名单？
**答案**：aria2 的协议支持是编译期可选的（u1-l2），白名单会随裁剪变化；而 `scheme://` 的结构判定（[src/uri_split.c:L98-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri_split.c#L98-L113)）天然把「本地路径」「magnet」排除在外。代价是任意 `foo://bar` 也会被当流协议，错误在后续连接阶段才暴露。
**练习 3**：`http://host/[01-03]/f` 展开成什么？
**答案**：`http://host/01/f`、`http://host/02/f`、`http://host/03/f`——START 与 END 等长（2 位），前导零被保留（[src/paramed_string.h:L131-L137](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/paramed_string.h#L131-L137) 用 `%02d` 格式化）。

### 4.4 DownloadHandler / DownloadHandlers：任务之间的衔接件

#### 4.4.1 概念说明

任务创建流水线不只服务「人敲命令」：Metalink 文件本身要先**下载**再解析、magnet 的 metadata 也要先**下载**。aria2 用两类处理器把这些「下载完之后/之前要做的事」抽象出来：

- **PreDownloadHandler**：在数据开始传输前改造当前任务（例如「这个响应其实是 Metalink/种子，改成下载到内存里，别写盘」）。
- **PostDownloadHandler**：在任务完成后**派生下一批 RequestGroup**。

两者都继承 `DownloadHandler`，靠可选的 `RequestGroupCriteria` 回答 `canHandle`。

#### 4.4.2 核心流程

```
RequestGroup 构造（非 --dry-run）
  └─ initializePreDownloadHandler / initializePostDownloadHandler
       按 --follow-torrent / --follow-metalink 的值（true|false|mem）挂处理器

运行期触发：
  HTTP：HttpResponseCommand 拿到响应头
        → fe->setContentType(...) → grp->preDownloadProcessing()   ← pre 时机
  FTP/SFTP：协商完成后同样触发 preDownloadProcessing
  完成：RequestGroupMan → group->postDownloadProcessing(nextGroups)
        → 匹配的 PostDownloadHandler::getNextRequestGroups 派生新组
```

`--follow-torrent`/`--follow-metalink` 三值语义：`false` 完全不挂；`true` 只挂 post（下载到磁盘文件，完成后解析该文件派生任务）；`mem` pre+post 都挂（响应直接进内存，跳过落盘）。

#### 4.4.3 源码精读

- [src/DownloadHandler.h:L48-L58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadHandler.h#L48-L58)：基类只持有一个 `criteria_` 并暴露 `canHandle`/`setCriteria`——判定逻辑完全外包给 criteria。
- [src/PreDownloadHandler.h:L42-L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PreDownloadHandler.h#L42-L49)：pre 的纯虚接口是 `execute(RequestGroup*)`——原地改造任务。
- [src/PostDownloadHandler.h:L44-L51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PostDownloadHandler.h#L44-L51)：post 的纯虚接口是 `getNextRequestGroups(groups, requestGroup)`——往 `groups` 里追加新任务。
- [src/download_handlers.cc:L57-L130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc#L57-L130)：`download_handlers` 命名空间是**进程级懒初始化单例工厂**：匿名命名空间里的 `unique_ptr` + `getXxx()` 首次调用时构造。共六个入口：内存 pre、Metalink pre/post、BT pre/post、UTMetadata post。pre 处理器都附带 criteria（后缀/Content-Type），post 处理器不需要 criteria（能挂上就处理）。
- [src/DownloadHandlerConstants.cc:L40-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadHandlerConstants.cc#L40-L67)：criteria 用的常量表——Metalink 后缀 `.metalink`/`.meta4`、Content-Type `application/metalink4+xml` 等；BT 后缀 `.torrent`、Content-Type `application/x-bittorrent`。
- [src/ContentTypeRequestGroupCriteria.cc:L52-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContentTypeRequestGroupCriteria.cc#L52-L72)：`match` 的两个前提——**单文件任务**（多文件任务不可能是 Metalink/种子本体）；然后按「后缀忽略大小写匹配」或「Content-Type 相等」命中即可。
- [src/RequestGroup.cc:L1084-L1114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1084-L1114)：挂接逻辑。`--follow-torrent == mem` 才挂 BT pre；`== true 或 mem` 挂 BT post（Metalink 同理）。它们在 RequestGroup 构造函数里被调用（[src/RequestGroup.cc:L156-L161](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L156-L161)），`--dry-run` 时跳过。
- [src/RequestGroup.cc:L1043-L1062](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1043-L1062)：`preDownloadProcessing`——遍历 preDownloadHandlers_，第一个 `canHandle` 的执行 `execute` 后返回。
- [src/RequestGroup.cc:L1064-L1082](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1064-L1082)：`postDownloadProcessing`——同理调用 `getNextRequestGroups`。
- 触发点一（HTTP 响应头到达）：[src/HttpResponseCommand.cc:L259-L270](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L259-L270)——在确定文件名之后、开写数据之前 `setContentType` + `preDownloadProcessing()`，这正是「Content-Type 是 Metalink 则就地转内存任务」的判定窗口。
- 触发点二（FTP/SFTP 协商完成）：[src/FtpNegotiationCommand.cc:L400](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L400)、[src/SftpNegotiationCommand.cc:L209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc#L209)。
- 触发点三（任务完成）：[src/RequestGroupMan.cc:L392](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L392)——`postDownloadProcessing(nextGroups)`，派生出的新组由 RequestGroupMan 插回等待队列（u3-l1 的 followedBy/following 链）。
- pre 处理器本体的行为：[src/MemoryPreDownloadHandler.h:L43-L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MemoryPreDownloadHandler.h#L43-L55)——换内存盘工厂、关文件分配与本地检查、标记 `markInMemoryDownload`、并发压到 1。magnet 任务（4.3.3）挂的 UTMetadataPostDownloadHandler 则在 metadata 收齐后调 `createRequestGroupForBitTorrent` 派生数据任务，完成闭环。

#### 4.4.4 代码实践

1. **实践目标**：观察 `--follow-metalink` 从 `true` 切到 `mem` 时任务行为的差异。
2. **操作步骤**：
   1. 准备一个可访问的 `.metalink`/`.meta4` URL（或本地 HTTP 服务托管一份；没有则标记待本地验证，改做步骤 4 的源码阅读）。
   2. `./src/aria2c --follow-metalink=true -l a.txt <metalink-url>`，完成后查看目录：会先出现 `.metalink`/`.meta4` 落盘文件，再出现派生的数据文件。
   3. `./src/aria2c --follow-metalink=mem -l b.txt <metalink-url>`：目录里只有数据文件，Metalink 本体不落盘。
   4. 源码阅读部分：对照 [src/RequestGroup.cc:L1092-L1097](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1092-L1097) 说明「`mem` 比 `true` 多挂了 pre 处理器」，再跟踪 [src/HttpResponseCommand.cc:L259-L270](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L259-L270) 中 `Content-Type: application/metalink4+xml` 如何命中 [src/ContentTypeRequestGroupCriteria.cc:L63-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContentTypeRequestGroupCriteria.cc#L63-L70) 的比较。
3. **需要观察的现象**：两次运行日志中任务数量（本体 + 派生）相同，但 `mem` 模式目录里没有 Metalink 文件；`a.txt` 日志能看到第一个任务的完成路径是 `.meta4` 文件。
4. **预期结果**：与 `--follow-metalink` 三值语义一致；若网络不可用，源码阅读结论（pre 只在 `mem` 挂载、触发点在响应头处理处）仍然可以从代码直接得出。

#### 4.4.5 小练习与答案

**练习 1**：为什么 pre 处理器的 criteria 要求「单文件任务」？
**答案**：[src/ContentTypeRequestGroupCriteria.cc:L55-L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContentTypeRequestGroupCriteria.cc#L55-L57)——Metalink/种子响应只可能对应一个刚协商出来的单文件下载；多文件任务（已是种子展开形态）不可能再「是」Metalink，提前排除避免误判。
**练习 2**：`--dry-run` 时为什么连 post 处理器都不挂？
**答案**：`--dry-run` 语义是「只走流程不落盘不派生」，挂了 post 会导致任务完成后继续解析并创建新任务，违背 dry-run 的预期；见 [src/RequestGroup.cc:L158-L161](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L158-L161)。
**练习 3**：magnet 任务挂了 `UTMetadataPostDownloadHandler`（4.3.3），它和本讲的 `createRequestGroupForBitTorrent` 如何衔接？
**答案**：metadata 任务完成后 [src/RequestGroupMan.cc:L392](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L392) 触发 postDownloadProcessing → UTMetadataPostDownloadHandler 拿内存中的 bencode 调 `createRequestGroupForBitTorrent`（[src/UTMetadataPostDownloadHandler.cc:L103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataPostDownloadHandler.cc#L103)）——**流水线的输出又变成流水线的输入**。

## 5. 综合实践

把本讲四个模块串成一个「预测—验证」实验。

**任务**：构造一个输入文件，让它一次触发 UriListParser、参数化展开、协议识别、post 派生四条路径；先在纸上写出预测，再运行核对。

```
# practice.txt
http://srv{1,2}/dir/file	http://srv3/dir/file

http://cdn[01-02]/big.iso
  split=4
  max-connection-per-server=2
```

（再用命令行追加一个本地种子：`./src/aria2c -i practice.txt -P -l log.txt --dry-run /path/to/test.torrent`，`test.torrent` 可用仓库自带的 [test/test.torrent](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/test.torrent)。）

**预测清单（动手前填写）**：

1. 参数化展开后条目一共有几条 URI？（`srv1`、`srv2`、`srv3` → 3 条）
2. 条目一、二各生成几个 RequestGroup？整个命令总共几个？（2 + 1 个种子 = 3，日志 `Downloading N item(s)` 验证）
3. 条目二里 `split=4`、`max-connection-per-server=2` 会不会生效？生效到谁身上？（initial option 白名单放行，作用在该条目的任务上；再结合 4.3 的 `splitURI` 公式算出该任务 FileEntry 里的 URI 份数）
4. `test.torrent` 为什么不会被并进条目一的镜像列表？（`stable_partition` 把它分到非流协议半区）

**验证**：运行后在 `log.txt` 中依次核对：`Downloading N item(s)` 的 N；各任务的 GID（是否重复即可观察 `--gid`/随机分配）；`srv1/srv2` 是否出现在 URI 列表；`test.torrent` 任务的 BT 日志（`--dry-run` 下至少能看到任务创建）。任何一处与预测不符，回到 4.2/4.3 的源码行号定位偏差原因。网络受限时，步骤 1、2、4 的结论仍可从源码与 [test/DownloadHelperTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadHelperTest.cc) 推出，标注待本地验证的部分留待有网环境补做。

## 6. 本讲小结

- 命令行模式下 `Context` 构造函数按 `--torrent-file` → `--metalink-file` → `--input-file` → 命令行 URI 的**互斥四级**调用 download_helper 的四个工厂；任务集合经 `MultiUrlRequestInfo::prepare` 移交 DownloadEngineFactory。
- 同一套工厂函数还服务 libaria2、RPC 与 post-download 派生（BtPostDownloadHandler/UTMetadataPostDownloadHandler/Metalink2RequestGroup）——「一处创建，六处复用」。
- `UriListParser` 定义了 `-i` 文件的行级语法：URI 行（Tab 分隔多镜像）+ 缩进选项行 + `#` 注释；条目级选项只有被 `setInitialOption(true)` 标注的白名单选项才生效，且命令行全局 `-o` 会先被剥离。
- `createRequestGroupForUri` 三步走：`-P` 参数化展开（`{}` 选择、`[]` 循环、前导零保留）→ `stable_partition` 把流协议 URI 合并为一个多镜像任务（`splitURI` 按 \(\min(\lfloor s/n\rfloor,m)\) 次整列表复制加余数补齐）→ 非流协议逐个经 `ProtocolDetector` 嗅探（`scheme://` 结构判定、magnet 试解析、本地文件读魔数 `d`/`<?xml`）。
- `-Z`（force-sequential）改变分组策略：每个 URI 独立成任务。
- `DownloadHandler` 体系用 pre（改造当前任务）/post（派生新任务）两类处理器衔接任务链，由 `--follow-torrent`/`--follow-metalink` 的 `true|mem|false` 控制，触发点分别在协议命令拿到响应/协商完成时与 RequestGroupMan 收尾时。

## 7. 下一步学习建议

- **u3-l4（SegmentMan 与分段调度）**：本讲结束时任务已经带着 `-s` 份 URI 与 `numConcurrentCommand` 就位，下一讲回答「数据到底怎么被切成 segment 分给各连接」。
- **u6-l1（torrent 元数据解析）**：本讲只走到 `bittorrent::loadFromMemory` 门口，种子字典如何变成 DownloadContext/TorrentAttribute 在该讲展开。
- **u8-l1/u8-l2（Metalink）**：`Metalink2RequestGroup` 背后的 SAX 状态机与 entry→RequestGroup 的映射。
- **u8-l4（RPC 方法分发）**：观察 `aria2.addUri` 如何带着本讲的 `ignoreLocalPath=true` 语义复用 `createRequestGroupForUri`。
- 继续阅读建议：直接精读 [src/download_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc) 中尚未展开的 `createBtMagnetRequestGroup`（配合 u6-l9），并把 [test/DownloadHelperTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadHelperTest.cc) 全部断言当作本讲的习题集。
