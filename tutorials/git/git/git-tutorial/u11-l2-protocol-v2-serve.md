# 协议 v2 与 serve

## 1. 本讲目标

本讲承接 u11-l1（transport/connect 与 pkt-line），从「客户端如何建连、如何用 pkt-line 分包」继续往上走一层，进入**协议版本 2（protocol v2）在服务端是如何被解析和分发的**。读完后你应该能够：

1. 说清协议 v2 相对 v0/v1 的五点关键改进，以及 v2「命令式（command-oriented）」设计的动机。
2. 顺着 `builtin/upload-pack.c` → `serve.c` 的调用链，讲清一次 v2 请求从 `version` 协商、能力通告（capability advertisement）、命令解析到 `command->command()` 执行的完整过程。
3. 读懂 `struct protocol_capability` 这张能力/命令注册表，分清「命令能力」「普通能力」「带值能力」三类，并说清 `ls-refs`、`object-info` 两个内置命令的实现。
4. 亲手用 `test-tool serve-v2` 驱动服务端，用 `GIT_TRACE_PACKET=1` 观察一次真实 fetch 的 v2 握手报文。

本讲只讲**服务端（server side）**的 v2 分发；客户端侧的协商算法（negotiator）与对象打包留给 u11-l3（fetch-pack / send-pack）。

## 2. 前置知识

在进入源码前，先用通俗语言建立四个概念。

**协议版本（protocol version）**。git 的线协议有三个版本：v0（最古老的，无版本号字符串）、v1（v0 基础上加一行 `version 1`）、v2（2018 年引入的重构版，本讲主角）。它们都跑在同一条传输管道上（u11-l1 讲的 `git://`、`ssh://`、`file://`、HTTP），区别只在「连上之后双方说什么话」。

**能力（capability）**。能力就是「我支持 X」或「请启用 X」的键值对。在 v0/v1 里，能力藏在第一行 ref 通告末尾、用一个 NUL 字节（`\0`）分隔，受单条 pkt-line 长度限制；v2 把能力提升为协议的「一等公民」，单独成段，可无限扩展。

**命令（command）**。v2 的核心创新：连接不再绑定到某个「服务」（如 `git-upload-pack`），而是连接到一个**可接受多个命令**的服务。客户端先看到服务通告了哪些**命令能力**（`ls-refs`、`fetch`、`object-info` 等），再用 `command=xxx` 选一个执行，执行完可在**同一条连接**上再发下一个命令。

**stateless-rpc（无状态 RPC）**。v2 默认无状态：服务端处理完一个命令就当自己「失忆」，不保留任何会话状态。这是为了让 HTTP 这种「每个请求可能落到不同后端」的部署能直接做负载均衡。`--stateless-rpc` 选项表达「我只做一次请求/应答交换就退出」。

如果你对 pkt-line 的 `0000`（flush）、`0001`（delim）、`0002`（response-end）控制包还不熟，请先回看 u11-l1，本讲会直接使用这些术语。

## 3. 本讲源码地图

本讲涉及的文件，按「从外到内」的调用顺序排列：

| 文件 | 作用 |
| --- | --- |
| [protocol.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/protocol.h) | 定义 `enum protocol_version` 与三个版本协商函数的声明 |
| [protocol.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/protocol.c) | 版本号字符串解析、客户端/服务端版本协商的实现 |
| [builtin/upload-pack.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/upload-pack.c) | `git-upload-pack` 子命令入口：协商版本后分流到 v0/v1/v2 |
| [serve.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.h) | 对外只暴露两个函数：通告能力、跑服务循环 |
| [serve.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.c) | **本讲核心**：能力通告、命令解析与分发的全部逻辑 |
| [ls-refs.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/ls-refs.c) | `ls-refs` 命令的实现（按前缀列出引用） |
| [protocol-caps.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/protocol-caps.c) | `object-info` 命令的实现（查对象大小） |
| [t/helper/test-serve-v2.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/t/helper/test-serve-v2.c) | 测试夹具：脱离网络、直接喂 pkt-line 给 serve 循环 |
| [Documentation/gitprotocol-v2.adoc](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Documentation/gitprotocol-v2.adoc) | v2 线协议的权威规范 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**协议 v2 命令模型**（讲设计动机与版本协商）、**serve 的能力通告与命令分发**（讲 serve.c 主循环）、**ls-refs 与能力声明**（讲一个完整命令是怎么实现的，顺带讲 object-info）。

### 4.1 协议 v2 命令模型：从「服务」到「命令」

#### 4.1.1 概念说明

要理解 v2，最好的办法是先看 v0/v1「为什么需要被换掉」。v0/v1 有几个长期痛点（详见规范文档开头的改进清单）：

- **每次连接必发全量 ref 通告**。哪怕你只想 fetch 一个分支，服务端也会把所有引用（refs）连同它们指向的对象哈希全列一遍。大型仓库（如 Linux 内核有上千个引用）这是巨大的浪费。
- **能力藏在 NUL 字节后面**。第一条 ref 行长这样：`<oid> HEAD\0<capability1> <capability2>...`，能力挤在 `\0` 之后，受单条 pkt-line 的 65520 字节上限约束，难以扩展。
- **一个服务名绑定一个动作**。`git-upload-pack` 管 fetch、`git-receive-pack` 管 push，二者协议各自演化。

v2 的回答是「**命令式（command oriented）**」：

- 连接建立后，服务端先发**能力通告**（capability advertisement），其中**一部分能力本身就是命令**（`ls-refs`、`fetch`、`object-info`、`bundle-uri`）。
- ref 通告**不再无条件发送**，而是要客户端显式用 `ls-refs` 命令请求，还能用 `ref-prefix` 只要一部分。
- 能力独立成段，每条一个 pkt-line，可任意扩展，不再受单行长度限制。
- 单条连接可顺序执行多个命令（除非走 `stateless-rpc`）。

一句话总结：**v0/v1 是「连上就倒一堆 ref 给你」，v2 是「你点哪个命令我才做什么」**。

#### 4.1.2 核心流程

版本协商是双向的，客户端和服务端各算一遍：

```
客户端                                  服务端
------                                  ------
get_protocol_version_config()           determine_protocol_version_server()
  读 protocol.version 配置(默认 v2)       读 GIT_PROTOCOL 环境变量里的 version=
  得到「我想用 v2」                        得到「客户端要 v2，我就用 v2」
        |                                      |
        +-> 通过 GIT_PROTOCOL 带上 version=2 ->+
                                                 |
                                       determine_protocol_version_client()
                                         客户端读服务端首行 "version 2" 确认
```

关键的**非对称性**要记住：

- 客户端默认值是 **v2**（`protocol.c` 里 `get_protocol_version_config()` 找不到配置就返回 `protocol_v2`）。
- 服务端默认值是 **v0**（`determine_protocol_version_server()` 在没收到任何 `version=` 时返回 `protocol_v0`）——这是为了兼容「根本不懂版本协商」的老客户端。

所以一次连接最终用哪个版本，取决于「客户端敢请求多新」和「客户端实际请求了什么」。新 git 客户端会请求 v2，于是服务端走 v2 分支。

#### 4.1.3 源码精读

先看版本枚举，只有三个真版本加一个「未知」：

[protocol.h:25-30](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/protocol.h#L25-L30) 定义 `enum protocol_version`，`unknown=-1` 专门表示「还没协商」。

服务端协商函数的核心在下面这段，它解析 `GIT_PROTOCOL` 环境变量（形如 `version=2:object-format=sha256`，用冒号分隔）：

[protocol.c:49-83](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/protocol.c#L49-L83) 是 `determine_protocol_version_server()`。它做三件事：用 `string_list_split` 按 `:` 拆 `GIT_PROTOCOL`；逐项 `skip_prefix(item->string, "version=", ...)`；**取客户端声明的最大版本**（`if (v > version) version = v`），因为客户端可能写 `version=0:version=2` 表示「我都行，你挑最先进的」。

而客户端配置默认值在这：

[protocol.c:21-47](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/protocol.c#L21-L47) 是 `get_protocol_version_config()`。先读 `protocol.version`，再退到测试用的 `GIT_TEST_PROTOCOL_VERSION` 环境变量，**都没有就返回 `protocol_v2`**（注意第 46 行，这就是「新 git 默认 v2」的来源）。

入口分流在 upload-pack：

[builtin/upload-pack.c:65-86](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/upload-pack.c#L65-L86) 用 `switch (determine_protocol_version_server())` 分三路：`protocol_v2` 走 `protocol_v2_advertise_capabilities` 或 `protocol_v2_serve_loop`；`protocol_v1` 只是先写一行 `version 1\n` 再 `fallthrough` 到 v0 的 `upload_pack`；`protocol_unknown_version` 直接 `BUG()`。注意 v1 和 v0 共享同一套 `upload_pack` 逻辑，只是 v1 多了个版本字符串——这正印证了「v1 是 v0 加版本号」。

#### 4.1.4 代码实践

**实践目标**：亲眼看版本协商的两端各取了什么值。

**操作步骤**：

1. 找两个本地仓库做实验（一个当「服务端」，一个当「客户端」），或对同一仓库自连。
2. 在客户端配置里强制版本：
   ```sh
   git -c protocol.version=2 ls-remote . 2>err.log
   ```
3. 打开 packet 追踪，观察首行：
   ```sh
   GIT_TRACE_PACKET=1 git -c protocol.version=2 ls-remote . 2>&1 | head -20
   ```

**需要观察的现象**：追踪输出里服务端第一条应是 `packet:  git< version 2`，随后是 `<capability>` 各行，最后 `0000`（flush）。把 `protocol.version` 改成 `0` 再跑一次，对比首行是否变成直接列 ref（无 `version` 行、无独立能力段）。

**预期结果**：v2 时握手以 `version 2` 开头、能力成段；v0 时直接是 `<oid> HEAD`。若你的 git 默认就输出 v2，说明 `get_protocol_version_config()` 返回了 `protocol_v2`，与源码一致。具体输出形式因平台/git 版本而异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `determine_protocol_version_server()` 要「取客户端声明的最大版本」而不是「取第一个」？

> **参考答案**：客户端可能声明多个可接受版本（如 `version=0:version=2`），表示「这些我都能讲」。规范假定最新版本最先进，故服务端挑最大的，确保双方都用上能力最强的版本。

**练习 2**：一个完全不发 `version=` 的老客户端连上来，服务端会走哪个分支？

> **参考答案**：`determine_protocol_version_server()` 默认返回 `protocol_v0`，于是 `builtin/upload-pack.c` 的 switch 走 v0 分支调 `upload_pack()`，完全向后兼容。

---

### 4.2 serve.c 的能力通告与命令分发

#### 4.2.1 概念说明

协商出 v2 之后，控制权交给 `serve.c`。这个文件是 v2 服务端的「大脑」，但它本身**不实现任何具体命令**——它只做三件事：

1. **通告能力**：把本服务支持的所有能力（含命令）列给客户端看。
2. **解析请求**：读客户端发来的 `command=xxx` 和一堆普通能力。
3. **分发执行**：校验后调用对应命令的 `command` 函数。

这是一种典型的**数据驱动（table-driven）分发**：所有能力集中登记在一张表里，主循环只查表、不写 `if/else`。新增一个命令只要往表里加一项、实现三个回调，主循环一行都不用改。

#### 4.2.2 核心流程

serve 对外只有两个入口（见 [serve.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.h)），由 `protocol_v2_serve_loop` 编排：

```
protocol_v2_serve_loop(r, stateless_rpc)
  |
  |-- (非 stateless) 先调 protocol_v2_advertise_capabilities(r)  # 发能力通告
  |
  `-- 循环 process_request(r):
        1. packet_reader 读客户端请求
        2. 逐行解析:
             - "command=xxx"   -> parse_command()   选定唯一命令
             - "key" / "key=v" -> receive_client_capability() 处理普通能力
             - 否则 die("unknown capability")
        3. 校验 object-format 一致
        4. command->command(r, &reader)  # 执行命令，由命令自己读完剩余参数
        5. stateless 时处理完即退出；否则回到第 1 步等下一条命令
```

请求的报文结构（来自规范）是固定的三段式：

```
command=<key>        <- 命令行（必须，且只能有一个）
<capability-list>    <- 普通能力（object-format=sha256 等）
0001                 <- delim 分隔
<command-args>       <- 命令专属参数（ref-prefix 等）
0000                 <- flush 结束
```

`process_request` 只负责前两段（命令 + 能力），第三段的命令参数**留给命令函数自己读**——这是为了让 serve 与具体命令解耦。

一个微妙的点：flush 包（`0000`）在「能力段」里**不会被吃掉**，而是原样留给孩子函数，让它用统一的方式看到「请求结束」。代码注释专门强调了这一点（见下方源码）。

#### 4.2.3 源码精读

**能力/命令的统一描述符**。每个能力用同一个结构体描述，三个回调字段任意组合：

[serve.c:103-138](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.c#L103-L138) 是 `struct protocol_capability`。关键字段含义：

- `advertise(r, &value)`：问「要不要通告这个能力？」，需要时把值写进 `value`（于是通告成 `name=value`）。注意 `value` 可能为 `NULL`——那只是「你支不支持？」的纯询问，不要求产出值。
- `command(r, request)`：**只有这个字段非 NULL，这个能力才是「命令」**。它负责读命令专属参数并产出响应。
- `receive(r, value)`：客户端把这个能力当**普通能力**发来时调用（如 `object-format=sha256` 里的 `sha256`）。

**能力注册表**。所有能力集中在这张静态数组里，这就是「数据驱动」的核心：

[serve.c:140-184](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.c#L140-L184) 是 `capabilities[]`。读这张表就能看清 v2 当前的能力全景：

- **命令能力**（有 `command`）：`ls-refs`、`fetch`、`object-info`、`bundle-uri`。
- **纯通告能力**（只有 `advertise`）：`agent`、`server-option`。
- **带 receive 的普通能力**：`object-format`、`session-id`、`promisor-remote`。

注意 `fetch` 的 `command` 指向 `upload_pack_v2`（实现在 upload-pack.c），而 `ls-refs`/`object-info` 分别指向 `ls_refs`/`cap_object_info`——serve 只持指针，不关心实现。

**能力通告函数**。这是客户端连上后看到的第一段输出：

[serve.c:186-216](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.c#L186-L216) 是 `protocol_v2_advertise_capabilities()`。先写 `version 2\n`，再遍历 `capabilities[]`：对每一项调 `advertise(r, &value)`，返回真才通告；若 `value` 非空就拼成 `name=value\n`，否则 `name\n`；最后 `packet_flush(1)` 收尾。这段代码正好对应规范里的 `capability-advertisement = protocol-version capability-list flush-pkt`。

**命令解析**。客户端用 `command=xxx` 选命令：

[serve.c:254-273](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.c#L254-L273) 是 `parse_command()`。三道校验：已选过命令再选就 `die("command ... requested after ...")`（一条请求只能一个命令）；选的不是命令（`!cmd->command`）就 `die("invalid command")`；带 `=value` 的命令也非法。这就是 t5701 里 `command=ls-refs=whatever` 会报 `invalid command` 的原因。

**普通能力接收**。命令之外的能力走另一条路：

[serve.c:241-252](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.c#L241-L252) 是 `receive_client_capability()`。注意第 246 行的 `c->command ||`——**如果一个能力是命令（有 command 字段），它就不能当普通能力收**。也就是说客户端不能在能力段写 `fetch`，只能写 `command=fetch`。这就把「命令」和「能力」在请求里的位置彻底分开。

**主请求处理循环**。这是分发的真正主干：

[serve.c:280-354](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.c#L280-L354) 是 `process_request()`。按状态机推进：

- 先 `packet_reader_peek` 偷看一行，若直接 `EOF` 说明客户端关连接，返回 1 让上层退出（第 296-297 行）。
- 进入循环后对每行分流：`parse_command` 或 `receive_client_capability` 命中即标记 `seen_capability_or_command`，否则 `die("unknown capability")`。
- 遇 `PACKET_READ_FLUSH`：若啥都没收到，视为客户端发空请求要断开（返回 1）；否则切到 `PROCESS_REQUEST_DONE`，**且故意不吃掉这个 flush**（第 314-331 行的注释解释了：要把 flush 留给命令函数，让它和「delim 后跟参数」的情形用同一套代码看到请求边界）。
- 遇 `PACKET_READ_DELIM`：直接进入 DONE（参数段交给命令读）。
- 最后校验 `object-format` 与服务端哈希算法一致（第 346-349 行，`object_format_receive` 存进 `client_hash_algo`，这里比对），再调 `command->command(r, &reader)` 执行。

**服务循环**。把上面串起来：

[serve.c:356-372](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.c#L356-L372) 是 `protocol_v2_serve_loop()`。有状态时先通告能力再 `for(;;)` 反复 `process_request`（返回 1 才 break）；`stateless_rpc` 时只处理一次就退出，且不主动通告能力（因为 stateless 模式下能力通告由 HTTP 层的 `--advertise-refs` 单独完成，见 `builtin/upload-pack.c` 第 67-70 行）。

#### 4.2.4 代码实践

**实践目标**：脱离网络，用 `test-tool serve-v2` 直接喂 pkt-line 给 serve 主循环，验证「未知能力」「无命令」「命令当能力」等分支。

**操作步骤**（需要先 `make` 编出 `test-tool`）：

```sh
cd /path/to/git-git && make && make -C t/helper test-tool 2>/dev/null

# 准备一个仓库
git init /tmp/serve-demo && cd /tmp/serve-demo
test_commit m1 2>/dev/null || { echo hi > a; git add a; git commit -m m1; }

# (1) 请求一个不存在的能力
printf 'foobar\n0000' | test-tool pkt-line pack \
  | test-tool serve-v2 --stateless-rpc 2>&1 | head

# (2) 只发能力不发命令
printf 'agent=git/test\nobject-format=sha1\n0000' | test-tool pkt-line pack \
  | test-tool serve-v2 --stateless-rpc 2>&1 | head
```

**需要观察的现象**：(1) 应在 stderr 看到 `unknown capability 'foobar'` 并以失败退出；(2) 应看到 `no command requested`。

**预期结果**：两条分别命中 `process_request` 里 `die("unknown capability")` 与 `die("no command requested")`，与 `t/t5701-git-serve.sh` 的 `request invalid capability`、`request with no command` 用例一致。具体退出码/措辞以本地 git 版本为准，**待本地验证**。

> 提示：`test-tool pkt-line pack` 把人类可读文本（`0000` 这类会被识别成控制包）打包成真正的 pkt-line 字节流，`serve-v2` 读取它再产出响应；这比走真实网络直观得多。该夹具实现见 [t/helper/test-serve-v2.c:15-39](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/t/helper/test-serve-v2.c#L15-L39)，它只是 `setup_git_directory` 后二选一调 `protocol_v2_advertise_capabilities` 或 `protocol_v2_serve_loop`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `receive_client_capability` 要拒绝「带 command 字段的能力」？

> **参考答案**：命令必须用 `command=` 显式选择（经 `parse_command`），且一条请求只能有一个命令。若允许把 `fetch` 当普通能力收，就会出现「能力段里塞了命令」与「command= 又选了命令」的歧义。用 `c->command` 这一项过滤，强行把命令和能力在语法上分开。

**练习 2**：`process_request` 看到 flush 包时为什么不像看到 delim 那样把它吃掉？

> **参考答案**：要让命令函数用「同一套代码」判断请求结束。无论客户端发的是「能力段直接 flush」（无参数命令）还是「能力段 delim + 参数段 flush」（有参数命令），命令函数都是「一直读到 flush 为止」。若 serve 提前吃掉 flush，无参数情形下命令函数就读不到结束信号了。

---

### 4.3 ls-refs 命令与能力声明（含 object-info）

#### 4.3.1 概念说明

`ls-refs` 是 v2 里替代「无条件全量 ref 通告」的命令。客户端用它**显式、带过滤地**要引用列表：

- `peel`：附带 peeled tag（把 annotated tag 解到指向的 commit）。
- `symrefs`：附带符号引用的目标（`symref-target:...`）。
- `ref-prefix <p>`：只要名字以 `p` 开头的引用——这是 v2 省流量的关键，fetch 时客户端只发自己关心的前缀。
- `unborn`：允许返回「尚未有提交」的分支（空仓库的默认分支）。

能力声明（capability declaration）是 v2 的扩展机制：一个命令的「附加特性」以**空格分隔的列表**作为该命令能力的值通告，例如 `ls-refs=unborn` 表示「我支持 unborn 选项」。客户端看到值里有 `unborn` 才会发 `unborn` 参数。

另一个内置命令 `object-info` 更简单：客户端给一批对象哈希，服务端返回它们的大小，**不传输对象内容**。这是给部分克隆（partial clone）场景用的——客户端有时只想知道对象多大，再决定要不要拉。

#### 4.3.2 核心流程

`ls-refs` 的处理是一条直线：

```
ls_refs(r, request):
  1. 读 uploadpack.hideRefs 配置 -> data.hidden_refs
  2. 循环 packet_reader_read 读参数: peel / symrefs / ref-prefix / unborn
     (读到 flush 为止，否则 die)
  3. 前缀过多(>= 65536)则全部丢弃(防 DoS，回退到全量)
  4. 先发 HEAD(可能 unborn)
  5. refs_for_each_ref_in_prefixes() 遍历, 每个引用回调 send_ref:
       - 跳过 hidden 引用
       - 不匹配前缀的跳过
       - 拼 "<oid> <name> [symref-target:..] [peeled:..]"
  6. packet_fflush 收尾
```

`object-info` 类似但更短：读 `size` 标志和一批 `oid <hash>`，用 `odb_read_object_info` 查大小，逐行回写。

#### 4.3.3 源码精读

**ls-refs 主函数**：

[ls-refs.c:161-216](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/ls-refs.c#L161-L216) 是 `ls_refs()`。逐段看：

- 第 171 行先 `repo_config(the_repository, ls_refs_config, &data)`，回调里（[ls-refs.c:148-159](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/ls-refs.c#L148-L159)）只认 `uploadpack.hideRefs`，收集隐藏引用。
- 第 173-189 行读参数循环：`peel`/`symrefs` 置位，`ref-prefix X` 追加前缀（且受 `TOO_MANY_PREFIXES` 上限保护，见第 48 行宏），`unborn` 走配置裁决。非法行直接 `die`。
- 第 191-192 行强制要求参数段以 flush 结尾。
- 第 199-200 行是**防 DoS 设计**：前缀超过 65536 个就全清空，回退成「不过滤」——因为前缀本是「穷举列表」，太多反而比不过滤还慢。
- 第 209-210 行 `refs_for_each_ref_in_prefixes(..., send_ref, &data)` 遍历引用，回调 `send_ref` 逐个输出。

**单引用输出回调**：

[ls-refs.c:78-121](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/ls-refs.c#L78-L121) 是 `send_ref()`。先 `strip_namespace` 去掉命名空间前缀，再 `ref_is_hidden` 过滤隐藏引用，`ref_match` 过滤前缀；然后拼串：`<oid> <name>`，可选 `symref-target:`（仅当客户端要 symrefs 且当前是符号引用）、`peeled:`（仅当客户端要 peel 且能剥）。第 92-94 行处理 unborn——`ref->oid` 为 NULL 时输出 `unborn <name>` 而非哈希。

**前缀匹配**：

[ls-refs.c:54-67](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/ls-refs.c#L54-L67) 是 `ref_match()`。没给前缀就全收（第 56-57 行），否则只要名字以任一前缀开头就算中。它配合 [refs.c:2043-2067](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2043-L2067) 的 `refs_for_each_ref_in_prefixes`，后者先求「最长前缀」再用引用后端的逐前缀迭代器，把扫描范围压到最小。

**能力声明（unborn）**：

[ls-refs.c:218-224](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/ls-refs.c#L218-L224) 是 `ls_refs_advertise()`。它在通告时根据 `lsrefs.unborn` 配置决定是否把 `unborn` 拼进值——于是通告成 `ls-refs=unborn`。`unborn_config`（[ls-refs.c:16-42](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/ls-refs.c#L16-L42)）默认 `advertise`，可配 `allow`（允许但不通告）/`ignore`（完全禁用）。这正是「命令附加特性以能力值通告」模式的实例。

**object-info 命令**：

[protocol-caps.c:80-115](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/protocol-caps.c#L80-L115) 是 `cap_object_info()`。读 `size` 标志与一批 `oid <hash>`，最后调 `send_info`。注意它默认是**关闭**的——`serve.c` 里 `object_info_advertise`（[serve.c:92-101](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/serve.c#L92-L101)）要 `transfer.advertiseobjectinfo` 配置为真才通告。

[protocol-caps.c:37-78](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/protocol-caps.c#L37-L78) 是 `send_info()`。对每个 oid 用 `odb_read_object_info(r->objects, &oid, &object_size)` 只查元数据、不读内容，把大小拼到行尾；查不到就留个空格。这正体现了 object-info「只问大小不拉对象」的定位。

#### 4.3.4 代码实践

**实践目标**：亲手发一个带 `ref-prefix` 的 `ls-refs` 请求，验证服务端只返回匹配的引用。

**操作步骤**（接 4.2.4 的 `/tmp/serve-demo`）：

```sh
cd /tmp/serve-demo
git branch dev 2>/dev/null; git tag v1 2>/dev/null

# 只想要 refs/heads/ 下的引用
{
  echo command=ls-refs
  echo object-format=sha1
  echo 0001                      # delim，分隔能力段与参数段
  echo ref-prefix refs/heads/
  echo 0000                      # flush 结束
} | test-tool pkt-line pack \
  | test-tool serve-v2 --stateless-rpc \
  | test-tool pkt-line unpack
```

**需要观察的现象**：输出里只有 `refs/heads/` 开头的引用（如 `refs/heads/master`、`refs/heads/dev`），**不会**出现 `refs/tags/v1`。

**预期结果**：与 `t/t5701-git-serve.sh` 的 `refs/heads prefix` 用例一致——`ref_match` + `refs_for_each_ref_in_prefixes` 把扫描范围限定在了 `refs/heads/`。把 `ref-prefix refs/heads/` 换成 `ref-prefix refs/tags/` 再跑，应只看到 tag。具体分支名取决于你的仓库，**待本地验证**。

> 想看真实网络版？这就是本讲主任务：
> ```sh
> GIT_TRACE_PACKET=1 git -c protocol.version=2 fetch . 2>&1 | grep -E 'version 2|command=|ref-prefix|<oid|0000|0001' | head -40
> ```
> 依次出现的 `version 2` → `command=ls-refs` → `ref-prefix ...` → flush、再 `command=fetch` 的顺序，就是 v2「命令交互」的真实节奏，对照 4.2.2 的流程图逐行核对即可。

#### 4.3.5 小练习与答案

**练习 1**：客户端发来 70000 个 `ref-prefix`，服务端会怎样？

> **参考答案**：`ls_refs` 收集前缀时受 `TOO_MANY_PREFIXES (65536)` 上限保护（第 182 行），超过后第 199-200 行直接 `strvec_clear` 清空所有前缀，等价于「不过滤、返回全部引用」。因为前缀语义是「穷举我要的」，太多时全量反而比逐个匹配更省，也避免被恶意客户端用来放大开销。

**练习 2**：通告 `ls-refs=unborn` 是怎么产生出来的？客户端要怎么配合？

> **参考答案**：`ls_refs_advertise` 在 `lsrefs.unborn` 为 `advertise`（默认）时把 `unborn` 拼进能力值，于是通告成 `ls-refs=unborn`。客户端看到值里有 `unborn`，才会在请求里发 `unborn` 参数；服务端 `ls_refs` 见到该参数且配置允许，就对空仓库的 HEAD 输出 `unborn <name>` 而非哈希。

**练习 3**：`object-info` 命令和 `fetch` 命令在「返回什么」上有什么本质区别？

> **参考答案**：`fetch` 传输对象内容（打包成 pack），`object-info` 只返回对象大小等元数据、绝不传内容（`odb_read_object_info` 只查不读）。前者用于真正拉取，后者用于部分克隆场景下「先问大小再决定拉不拉」。

---

## 5. 综合实践

把三个模块串起来，做一个「迷你 v2 协议观察员」任务。

**任务**：在 `/tmp/serve-demo` 上完成下面三步，并把每一步对应到 `serve.c` 的代码行。

1. **能力通告**：`test-tool serve-v2 --advertise-capabilities | test-tool pkt-line unpack`。记下输出里出现的命令能力有哪些（应有 `ls-refs`、`fetch`，`object-info` 多半没有因为默认关）。→ 对应 `protocol_v2_advertise_capabilities`（serve.c:186-216）。

2. **一次完整 ls-refs**：发 `command=ls-refs` + `object-format=sha1` + delim + `peel` + `symrefs` + `ref-prefix refs/heads/` + flush，解包输出，确认每行格式是 `<oid> <name> [symref-target:..] [peeled:..]`。→ 对应 `process_request`（serve.c:280-354）分发到 `ls_refs`（ls-refs.c:161-216）。

3. **真实 fetch 抓包**：`GIT_TRACE_PACKET=1 git -c protocol.version=2 fetch . 2>&1`。在输出里标注出：① `version 2` 协商；② 第一条 `command=ls-refs`（列引用）；③ 第二条 `command=fetch`（拉对象）。→ 这就是一条连接上**顺序执行两个命令**的实证，正是 v2 相对 v0/v1 的核心改进。

**验收标准**：你能用自己的话讲清「客户端发 `version=2` → 服务端通告能力 → 客户端 `command=ls-refs` 拿到引用 → 客户端 `command=fetch` 拉对象」这条完整链路里，每一步分别由 `protocol.c`、`serve.c`、`ls-refs.c`、`upload-pack.c` 的哪个函数负责。

## 6. 本讲小结

- 协议 v2 是**命令式（command-oriented）**设计：一条连接、一个服务、多个命令，ref 通告从「无条件全量」变成「`ls-refs` 显式按前缀请求」，能力从「藏在 NUL 后」变成「独立成段」，天然适配 stateless-rpc/HTTP。
- 版本协商双向各算一次：客户端默认 v2（`get_protocol_version_config`），服务端从 `GIT_PROTOCOL` 取客户端声明的最大版本、默认 v0 兜底（`determine_protocol_version_server`），由 `builtin/upload-pack.c` 的 switch 分流。
- `serve.c` 用一张 `struct protocol_capability` 注册表做**数据驱动分发**：`advertise` 决定通告、`command` 标记是否命令、`receive` 处理普通能力；主循环 `process_request` 只查表、校验、调 `command->command()`。
- 命令和能力在请求里**语法分离**：命令必须 `command=` 选（且仅一个），带 `command` 字段的能力不能当普通能力收（`receive_client_capability` 的 `c->command` 过滤）。
- `ls-refs` 用 `ref-prefix` + `refs_for_each_ref_in_prefixes` 做按需过滤，用 `TOO_MANY_PREFIXES` 防 DoS；其附加特性（`unborn`）以能力值 `ls-refs=unborn` 通告，是 v2 能力扩展机制的典型样例。
- `object-info` 只返回对象大小、不传内容，服务于部分克隆；默认关闭，受 `transfer.advertiseobjectinfo` 控制。

## 7. 下一步学习建议

本讲只讲了**服务端如何分发 v2 命令**，但 `command=fetch` 真正执行时的「对象协商」还没有展开。下一讲 **u11-l3（fetch-pack / send-pack 协商）** 会进入：

- `fetch-pack.c` 客户端如何决定向服务端通告（advertise）哪些已有 commit，让服务端据此算出「还缺哪些对象」。
- `fetch-negotiator.c` 与 `negotiator/default.c` 的通告算法。
- `send-pack.c`（推送侧）与服务端 `upload_pack_v2` 如何打包对象。

建议同时带着本讲的认知重读 `serve.c` 的 `capabilities[]` 表里 `fetch` 那一项——它的 `command` 指向 `upload_pack_v2`，那正是 u11-l3 的起点。若想从更高层看传输抽象如何选择后端，可回看 u11-l1 的 `transport.c`。
