# 命名管道通道 PipeChannel

## 1. 本讲目标

本讲是「IPC 骨架」单元的第二讲。上一讲（u2-l1）我们已经看清 IPC 层的「合同」：`Client` / `Server` / `RequestHandler` 三大抽象、`WEASEL_IPC_COMMAND` 命令枚举、定长的 `PipeMessage`、以及那张「方法↔命令↔派发函数↔虚函数」映射表。但那只是接口层；真正把一个命令字节流从 WeaselTSF 进程搬到 WeaselServer 进程的，是本讲的主角——`PipeChannel`。

学完本讲你应该能够：

- 说清 `PipeChannel` 这个类模板的四个模板参数各自控制什么，以及客户端与服务端为何把参数「反过来」写。
- 画出一次 `Transact(msg)` 的完整时序：写消息体 → `_Send` → 服务端 `Listen` → `_ReceiveResponse`，并指出缓冲与线程本地存储出现的位置。
- 解释 `PipeChannelBase` 如何管理「按需连接 / 断线重连 / 消息模式读写」，以及它用 `DWORD` 异常码传递错误的风格。
- 理解为什么管道句柄和缓冲区要用 `boost::thread_specific_ptr` 做线程本地隔离，以及 `wbufferstream` 如何把文本直接写进管道缓冲。

---

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 Windows 命名管道（Named Pipe）

命名管道是 Windows 提供的跨进程通信原语，形如 `\\.\pipe\<用户名>\WeaselNamedPipe`。它有两端：

- **服务端**：用 `CreateNamedPipe` 创建一个管道实例，再用 `ConnectNamedPipe` 阻塞等待客户端连入。一个管道名可以挂多个实例（`PIPE_UNLIMITED_INSTANCES`），从而支持多个客户端同时连接。
- **客户端**：用 `CreateFile(..., OPEN_EXISTING, ...)` 像打开文件一样连上某个实例。

管道有两种读写模式：**字节模式（BYTE）** 和 **消息模式（MESSAGE）**。Weasel 用的是消息模式（`PIPE_READMODE_MESSAGE`）：一次 `WriteFile` 写入的内容在接收端会被当作「一条完整消息」边界保留。本讲会反复遇到 `ERROR_MORE_DATA` 这个错误码——它不是真的出错，而是「这条消息比你的接收缓冲还长，剩余字节还在管道里，请继续读」。

> 小狼毫的管道名按用户名隔离，由 `GetPipeName()` 拼出（见 [include/WeaselIPC.h:170-177](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/WeaselIPC.h#L170-L177)），这在 u2-l1 已讲过，本讲不再展开。

### 2.2 「请求头 + 文本正文」的双层载荷

回忆 u2-l1：管道上每次往返的定长头部是 `PipeMessage{Msg, wParam, lParam}`（命令 + 两个 DWORD 参数）。但很多命令还需要带一段**变长文本正文**——例如 `START_SESSION` 要带「客户端应用名」、`PROCESS_KEY_EVENT` 的响应要带回「候选词文本」。

`PipeChannel` 把这两层放进同一块缓冲区：头部是定长结构体，正文是紧随其后的宽字符文本。本讲要回答的核心问题之一就是：**这块缓冲区是怎么被组织、被写入、被读取的？**

### 2.3 线程本地存储（Thread-Local Storage, TLS）

WeaselTSF 是一个被加载进**每个**应用进程的 DLL，TSF（文本服务框架）可能从多个线程调用它；WeaselServer 则为每个接入的管道实例派生一个工作线程。无论哪一侧，同一个管道句柄若被多线程并发读写，消息边界就会错乱。

解决办法是 TLS：每个线程拥有**自己的一份**管道句柄和缓冲区，互不干扰。Weasel 用 `boost::thread_specific_ptr` 实现，本讲的第三个最小模块就围绕它展开。

---

## 3. 本讲源码地图

本讲只盯住「通道」本身，涉及的关键文件不多：

| 文件 | 作用 |
| --- | --- |
| [include/PipeChannel.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h) | `PipeChannelBase`（连接管理基类）与 `PipeChannel` 类模板的全部声明，包含内联的 `Transact`/`_Send`/`_ReceiveResponse` 等。 |
| [WeaselIPC/PipeChannel.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp) | `PipeChannelBase` 的成员实现：`_Ensure`/`_Connect`/`_TryConnect`/`_WritePipe`/`_Receive`/`_ConnectServerPipe` 等。 |
| [WeaselIPC/WeaselClientImpl.h](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.h) / [.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp) | 客户端如何**使用** `PipeChannel<PipeMessage>`，是理解模板参数的最佳样本。 |
| [WeaselIPCServer/WeaselServerImpl.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp) | 服务端 `PipeServer`（继承自 `PipeChannel<DWORD, PipeMessage>`）的 `Listen`/`_ProcessPipeThread`，是「反过来用模板」的样本。 |

> 提醒：`WeaselClientImpl` / `WeaselServerImpl` 的**业务封装**（如何把每个 IPC 命令映射到通道调用）是下一讲 u2-l3 的主题。本讲只在需要佐证通道行为时引用它们。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **PipeChannelBase 连接管理** —— 把命名管道的创建、连接、重连、读写包成一组可复用原语。
2. **PipeChannel 模板 Transact 流程** —— 在基类之上，用模板把「定长头 + 变长正文」的请求-响应模型定型为 `Transact`。
3. **线程本地句柄与缓冲流** —— 用 TLS 隔离每线程的连接，用 `wbufferstream` 把文本写进缓冲。

### 4.1 PipeChannelBase 连接管理

#### 4.1.1 概念说明

`PipeChannelBase` 是一个**与消息类型无关**的基类：它只懂「有一个命名管道、有一个缓冲、要在上面读写」，但不关心读写的是什么结构。把「消息类型」抽走的好处是，客户端和服务端可以共用同一套连接逻辑——它们对管道的需求在传输层是完全对称的。

它对外（其实是 `protected`，供子类使用）提供的能力可以分成三组：

- **连接生命周期**：`_Ensure`（确保已连）、`_Connect`（以客户端身份连）、`_TryConnect`（尝试连一次）、`_Reconnect`（断线重连）、`_FinalizePipe`（关闭）。
- **数据搬运**：`_WritePipe`（写并刷新）、`_Receive`（读一条消息，含 `ERROR_MORE_DATA` 处理）。
- **服务端专用**：`_ConnectServerPipe`（创建实例并等待客户端连入）。

#### 4.1.2 核心流程

客户端建立连接的伪代码：

```
_Ensure():
    若线程本地句柄无效:
        句柄 = _Connect(管道名)
    返回句柄是否有效

_Connect(name):                # 阻塞直到连上
    循环:
        pipe = _TryConnect()    # CreateFile 打开已有管道
        若 pipe 无效:
            WaitNamedPipe(name, 500ms)   # 等服务端腾出实例
        否则 break
    把 pipe 设为 MESSAGE 读模式
    返回 pipe

_TryConnect():
    pipe = CreateFile(管道名, 读写, OPEN_EXISTING)
    若 pipe 有效: 返回 pipe
    否则要求错误码恰为 ERROR_PIPE_BUSY（实例忙，可重试）
       其它错误码 → 抛 DWORD 异常
```

写一条消息（`_WritePipe`）伪代码：

```
_WritePipe(pipe, 字节数, 缓冲):
    若 WriteFile 失败或写入 0 字节: 抛 GetLastError()
    FlushFileBuffers(pipe)        # 确保对方能立刻读到
```

读一条消息（`_Receive`）伪代码：

```
_Receive(pipe, 接收缓冲, 期望头部长度 rec_len):
    success = ReadFile(pipe, 接收缓冲, rec_len)
    若 success == false:
        要求错误码恰为 ERROR_MORE_DATA（消息比 rec_len 长）
        清空整块上下文缓冲
        再 ReadFile 把「剩余正文」读进上下文缓冲
    标记 has_body = false
```

服务端则反过来用 `_ConnectServerPipe`（`CreateNamedPipe` + `ConnectNamedPipe`）守株待兔，详见 4.2 节的服务端样本。

#### 4.1.3 源码精读

先看基类的成员骨架与「按需连接」入口。基类把管道名、缓冲大小、安全属性存为成员，管道句柄和上下文则是**线程本地**的（详见 4.3 节）：

[include/PipeChannel.h:57-68](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L57-L68) —— `pname`（管道名）、`buff_size`（缓冲容量）、`sa`（安全属性）是普通成员；`hpipe_ptr` 与 `context` 是线程本地指针。

`_Ensure` 是一切读写的前置动作，逻辑很短但很关键：

[WeaselIPC/PipeChannel.cpp:27-39](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L27-L39) —— 若线程本地句柄仍是 `INVALID_HANDLE_VALUE`，就调用 `_Connect` 真正去连；任何异常都被吞掉并返回 `false`（连接失败不崩进程）。

`_Connect` 用「忙等 + `WaitNamedPipe`」争取一个实例，并把句柄切到消息模式：

[WeaselIPC/PipeChannel.cpp:41-50](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L41-L50) —— `while` 循环里反复 `_TryConnect`，失败就 `WaitNamedPipe(name, 500)` 等 500 毫秒；连上后用 `SetNamedPipeHandleState` 设 `PIPE_READMODE_MESSAGE`，失败即抛错。

`_TryConnect` 揭示了「`ERROR_PIPE_BUSY` 不是错误」这一关键约定：

[WeaselIPC/PipeChannel.cpp:58-69](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L58-L69) —— `CreateFile` 打开服务端的管道实例；若返回无效，用 `_ThrowIfNot(ERROR_PIPE_BUSY)` 断言「当前错误码必定是实例繁忙」，否则把真实错误码抛出去。

读写与清理的细节：

[WeaselIPC/PipeChannel.cpp:71-78](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L71-L78) —— `_WritePipe`：`WriteFile` 后紧跟 `FlushFileBuffers`，保证对端能立即读到，这对输入法的低延迟体感很重要。

[WeaselIPC/PipeChannel.cpp:80-86](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L80-L86) —— `_FinalizePipe`：`DisconnectNamedPipe` + `CloseHandle`，并把句柄重置为 `INVALID_HANDLE_VALUE`。

[WeaselIPC/PipeChannel.cpp:88-102](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L88-L102) —— `_Receive`：先按 `rec_len` 读头部；若返回失败，用 `_ThrowIfNot(ERROR_MORE_DATA)` 断言「只是消息更长」，然后把剩余正文读进 `ctx->buffer`。

最后看错误处理的风格——基类用一组宏把 `GetLastError()` 直接当异常抛：

[WeaselIPC/PipeChannel.cpp:9-16](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L9-L16) —— `_ThrowLastError` 抛 `GetLastError()`，`_ThrowCode(c)` 抛指定码，`_ThrowIfNot(c)` 表示「若当前错误码不是 c 就抛」。抛出的是 `DWORD`，这正是为什么客户端的 `_SendMessage` 用 `catch (DWORD)` 接住（见 [WeaselIPC/WeaselClientImpl.cpp:193-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L193-L202)）。

#### 4.1.4 代码实践

**实践目标**：把「客户端连不上」时的重试与等待行为，对照源码走一遍，建立对 `_Connect` 循环的肌肉记忆。

**操作步骤**（源码阅读型）：

1. 打开 [WeaselIPC/PipeChannel.cpp:41-50](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L41-L50) 的 `_Connect`。
2. 假设 WeaselServer 尚未启动，客户端调用 `channel.Connect()`（即 `_Ensure` → `_Connect`）。逐步推演：
   - 第一次 `_TryConnect`：`CreateFile` 对一个不存在的管道名会返回什么错误码？（提示：`ERROR_FILE_NOT_FOUND`，而非 `ERROR_PIPE_BUSY`。）
   - 走到 `_ThrowIfNot(ERROR_PIPE_BUSY)` 时会发生什么？
3. 再假设服务端已启动但所有实例都被占用：`_TryConnect` 返回 `INVALID_HANDLE_VALUE` 且错误码为 `ERROR_PIPE_BUSY`，于是进入 `WaitNamedPipe(name, 500)`。

**需要观察的现象 / 预期结果**：

- 服务端未启动时，`_Connect` 的 `while` 循环**不会**无限自旋——因为 `_TryConnect` 在「管道根本不存在」时会抛出非 `ERROR_PIPE_BUSY` 的码，异常冒泡到 `_Ensure` 的 `catch(...)` 被吞掉，`_Ensure` 返回 `false`。这正是客户端 `Connect()` 返回 `false`、上层据此决定「启动 Server 进程」的依据（详见 u2-l3）。
- 服务端繁忙时，客户端会以 500ms 为单位耐心等待实例空闲。

> 待本地验证：若你在 Windows 上用调试器单步 `WeaselClientImpl::Connect`，可在 `_TryConnect` 的 `CreateFile` 返回处观察到上述两种错误码的分支差异。

#### 4.1.5 小练习与答案

**练习 1**：`_Ensure` 里的 `try{...}catch(...){return false;}` 把所有异常都吞掉，这样设计有什么好处和代价？

**参考答案**：好处是连接失败不会让宿主进程（记事本、Word 等）崩溃，输入法「最坏情况就是打不出中文」而不是「拖垮应用」；代价是上层无法区分「服务未启动」「权限不足」「管道损坏」等不同原因，只能拿到一个布尔结果，需要靠后续命令重试或启动服务来兜底。

**练习 2**：`_Receive` 中如果把 `_ThrowIfNot(ERROR_MORE_DATA)` 这一行删掉，会发生什么？

**参考答案**：任何 `ReadFile` 失败（包括真正的网络/管道破裂错误）都会被当作「消息更长」处理，紧接着去读「剩余正文」。这会把一个本该中止的致命错误伪装成一次正常读取，可能让调用方拿到半截乱码正文却还以为成功。该断言是「错误码契约」的防线。

---

### 4.2 PipeChannel 模板 Transact 流程

#### 4.2.1 概念说明

`PipeChannelBase` 只懂字节流，不知道「头部结构体长什么样」「响应是什么类型」。`PipeChannel` 这个类模板把这些类型信息补齐，并把「发一条请求、收一条响应」固化为一个方法：`Transact(msg)`。

看模板签名：

[include/PipeChannel.h:71-74](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L71-L74) —— 四个模板参数：

- `_TyMsg`：**本端发送**的消息头部类型；
- `_TyRes`：**本端接收**的响应头部类型，默认 `DWORD`；
- `_MsgSize`：发送头大小，默认 `sizeof(_TyMsg)`；
- `_ResSize`：接收头大小，默认 `sizeof(_TyRes)`。

正是这组参数让客户端和服务端可以「镜像」地复用同一个模板：

- **客户端** `PipeChannel<PipeMessage>`：发送 `PipeMessage`（请求头），接收 `DWORD`（结果码）。对应代码 [WeaselIPC/WeaselClientImpl.h:46](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.h#L46)。
- **服务端** `PipeChannel<DWORD, PipeMessage>`：发送 `DWORD`（结果码），接收 `PipeMessage`（请求头）。对应代码 [WeaselIPCServer/WeaselServerImpl.cpp:9](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L9)。

也就是说，`Msg`/`Res` 是**站在本端视角**的「我发什么 / 我收什么」，所以两端把参数顺序写反。这是理解整段代码的钥匙。

> 小贴士：模板里还有一个枚举 `enum class ChannalCommand { NEW_MSG_PIPE, REFRESH }`（[include/PipeChannel.h:84](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L84)，源码中拼写为 `ChannalCommand`），它声明了通道内部命令的两个取值。需要说明的是：在当前代码库里**没有**任何地方引用该枚举（搜索 `ChannalCommand`/`NEW_MSG_PIPE` 仅命中这一处声明），属于已定义但当前未使用的预留接口，阅读时可暂时跳过。

#### 4.2.2 核心流程

一次客户端 `Transact(msg)` 的全流程：

```
Transact(msg):
    _Ensure()                       # 4.1 节：确保线程本地句柄可用
    phandle = _GetPipeHandle()      # 取线程本地句柄
    _Send(*phandle, msg)            # 把「头部 + 之前用 << 累积的正文」一起写出
    return _ReceiveResponse()       # 读回响应头部（DWORD）+ 可选正文

_Send(pipe, msg):
    把 msg 拷到缓冲区开头             # 定长头
    若 has_body: 用 write_stream 的 tellp 算出正文字节数
    data_sz = 头大小 + 正文字节数（不超过 buff_size）
    try: _WritePipe(pipe, data_sz, 缓冲)
    catch: _Reconnect(); _WritePipe(...)   # 断线重连后重发一次
    ClearBufferStream()             # 清掉正文，准备下一次往返

_ReceiveResponse():
    phandle = _GetPipeHandle()
    result: Res
    _Receive(*phandle, &result, sizeof(result))   # 先读头部；正文留待 HandleResponseData 取
    return result
```

注意几个设计要点：

- **正文是「提前累积」的**：调用方在 `Transact` 之前用 `channel << 文本` 把正文写进缓冲，`_Send` 再把它们连同头部一次发出。
- **正文长度靠流的位置算**：`_Send` 用 `write_stream->tellp()` 拿到已写入的宽字符数，乘以 `sizeof(wchar_t)` 换算成字节。
- **写失败自动重连重发一次**：`_WritePipe` 抛出异常时，`_Send` 捕获后 `_Reconnect()` 再写一次，给瞬断一次自愈机会。
- **响应正文延迟取**：`_ReceiveResponse` 只把头部读回来；正文（如候选词文本）已被 `_Receive` 放进上下文缓冲，由后续 `HandleResponseData(handler)` 交给业务回调。

#### 4.2.3 源码精读

`Transact` 本体只有三行，但它是整个客户端的「心脏」：

[include/PipeChannel.h:120-125](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L120-L125) —— `_Ensure()` → `_Send(*phandle, msg)` → `return _ReceiveResponse()`。

`_Send` 是「头部 + 正文」拼装与容错的核心：

[include/PipeChannel.h:151-175](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L151-L175) —— 关键几句：

```cpp
*reinterpret_cast<Msg*>(pbuff) = msg;        // 头部写到缓冲区开头
...
std::streampos pos = ctx->write_stream->tellp();
body_bytes = static_cast<size_t>(pos) * sizeof(wchar_t);   // 正文wchar数→字节
size_t data_sz = ctx->has_body ? (_MsgSize + body_bytes) : _MsgSize;
if (data_sz > buff_size) data_sz = buff_size;              // 上限保护
try { _WritePipe(pipe, data_sz, pbuff); }
catch (...) { _Reconnect(); _WritePipe(pipe, data_sz, pbuff); }  // 重连重发
ClearBufferStream();
```

正文的写入入口是 `Write` 与 `operator<<`，它们把内容送进一个**延迟创建**的 `wbufferstream`：

[include/PipeChannel.h:107-118](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L107-L118) —— `Write(cnt)` 置 `has_body = true` 并 `<< cnt` 进流；`operator<<` 只是对 `Write` 的链式包装。

[include/PipeChannel.h:184-193](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L184-L193) —— `_BufferWriteStream()`：首次访问时，在「缓冲区 + `_MsgSize`」位置上零拷贝构造一个 `wbufferstream`（先把该区域 `memset` 清零）。

`_ReceiveResponse` 把读回的头部交给基类 `_Receive`：

[include/PipeChannel.h:177-182](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L177-L182) —— 申请一个 `Res result`，`_Receive(*phandle, &result, sizeof(result))`，返回 `result`。

响应正文的取出由 `HandleResponseData` 完成——它把整块上下文缓冲交给业务回调：

[include/PipeChannel.h:139-148](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L139-L148) —— 把 `ctx->buffer.get()` 当作 `LPWSTR`、长度按 `buff_size*sizeof(char)/sizeof(wchar_t)` 传给 `handler`。

`SendBuffer` 与 `ReceiveBuffer` 暴露缓冲区里「正文区域」的位置：

[include/PipeChannel.h:135-137](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L135-L137) —— `SendBuffer()` 返回 `buffer + _MsgSize`（发送正文区起点，正是 `_BufferWriteStream` 建流的位置）；`ReceiveBuffer()` 返回 `buffer + _ResSize`（接收正文区起点，服务端用它把请求正文交给业务层）。

来看两端真实用法。客户端发 `START_SESSION` 前，先用 `<<` 把客户端信息写进正文：

[WeaselIPC/WeaselClientImpl.cpp:185-191](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L185-L191) —— 连续 `channel << L"action=session\n"` 等，累积请求正文。

随后 `_SendMessage` 把命令打包成 `PipeMessage` 并 `Transact`：

[WeaselIPC/WeaselClientImpl.cpp:193-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L193-L202) —— `PipeMessage req{Msg, wParam, lParam}; return channel.Transact(req);`，并用 `catch (DWORD)` 吸收通道抛出的错误码、返回 0 表示失败。

服务端的镜像用法更直观地体现了「参数反过来」。`PipeServer` 继承 `PipeChannel<DWORD, PipeMessage>`，于是它的 `Msg=DWORD`、`Res=PipeMessage`：每个连接对应一个工作线程，循环里 `_Receive` 收一条 `PipeMessage` 请求，处理后再 `_Send` 一个 `DWORD` 结果回去：

[WeaselIPCServer/WeaselServerImpl.cpp:428-438](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L428-L438) —— `_ProcessPipeThread`：`Res msg; _Receive(pipe, &msg, sizeof(msg));` 收请求；`handler(msg, [this,pipe](Msg resp){ _Send(pipe, resp); });` 处理后回写结果。

服务端同样用 `*channel << msg` 往正文区写响应文本（比如候选词）。以按键处理为例，`eat` 回调就是一个「写正文」的闭包：

[WeaselIPCServer/WeaselServerImpl.cpp:215-226](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L215-L226) —— `auto eat = [this](std::wstring& msg) -> bool { *channel << msg; return true; };`，再交给 `m_pRequestHandler->ProcessKeyEvent(...)`。这个 `eat` 正是 u2-l1 讲过的 `EatLine` 回调。

服务端读请求正文则用 `ReceiveBuffer()`，把它交给业务层解析客户端信息：

[WeaselIPCServer/WeaselServerImpl.cpp:194-205](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L194-L205) —— `AddSession(reinterpret_cast<LPWSTR>(channel->ReceiveBuffer()), eat...)`；其解析逻辑见 [RimeWithWeasel/RimeWithWeasel.cpp:408-427](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/RimeWithWeasel/RimeWithWeasel.cpp#L408-L427)，按行寻找 `session.client_app=`。

> 注意一个不对称：客户端 `_Send` 用的是**线程本地句柄**（`_GetPipeHandle()`），而服务端 `_Send` 用的是**显式传入的 `pipe`**（每个连接自己那份）。这是因为服务端一个对象要同时管多个连接，必须显式指定写哪一个；客户端每个线程只维护一条连接，故可藏在线程本地存储里。

#### 4.2.4 代码实践

**实践目标**：以一次 `PROCESS_KEY_EVENT` 为样本，把客户端「写正文 → 发头部」与服务端「收请求 → 写响应正文 → 发结果」的两段对称流程对齐。

**操作步骤**（源码阅读 + 画图型）：

1. 客户端侧：阅读 [WeaselClientImpl.cpp:58-65](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L58-L65) 的 `ProcessKeyEvent`，确认它调用 `_SendMessage(WEASEL_IPC_PROCESS_KEY_EVENT, keyEvent, session_id)`，并注意此时**没有**用 `<<` 写正文（按键请求不需要正文）。
2. 进入 `_SendMessage`（[L193-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/WeaselClientImpl.cpp#L193-L202)），追到 `channel.Transact(req)`。
3. 在 `_Send`（[PipeChannel.h:151-175](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L151-L175)）里确认：因为 `has_body==false`，`data_sz` 就等于 `_MsgSize`，只发了 12 字节的 `PipeMessage` 头。
4. 服务端侧：进入 `_ProcessPipeThread`（[WeaselServerImpl.cpp:428-438](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L428-L438)）→ `HandlePipeMessage` → `OnKeyEvent`（[L215-226](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L215-L226)）。注意 `eat` 回调会在处理过程中用 `*channel << msg` 把候选词正文写进服务端的正文区。
5. 回到 `_ProcessPipeThread` 的 `resp(result)`：它调用 `_Send(pipe, resp)`，此时服务端 `has_body==true`，于是把 `[DWORD 结果][候选词正文]` 一次性写出。
6. 客户端 `_ReceiveResponse` 读回 `DWORD`；随后 `GetResponseData` → `HandleResponseData` 把正文交给 UI。

**需要观察的现象 / 预期结果**：

- 按键请求方向**只发头部**（无正文），响应方向**既发结果码又发正文**；这与 `START_SESSION`（请求带正文、响应不带）恰好相反。两端共用一套 `_Send` 逻辑，靠 `has_body` 自动适应。
- 服务端写正文与发结果**在同一线程、同一上下文缓冲**里完成，先 `<<` 累积、后 `_Send` 一次发出。

> 待本地验证：若在 `_Send` 的 `_WritePipe` 调用处下断点，可观察 `data_sz` 在「按键请求」与「带候选的响应」两种情形下的差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么客户端模板写成 `PipeChannel<PipeMessage>`，而服务端写成 `PipeChannel<DWORD, PipeMessage>`？

**参考答案**：`Msg` 是「本端发送的头部类型」，`Res` 是「本端接收的头部类型」。客户端发 `PipeMessage` 请求、收 `DWORD` 结果，所以是 `<PipeMessage>`（`Res` 取默认 `DWORD`）；服务端发 `DWORD` 结果、收 `PipeMessage` 请求，所以把两个参数都显式写出且顺序相反。两者其实描述的是同一条链路上的相反方向。

**练习 2**：`_Send` 里 `data_sz` 被限制为不超过 `buff_size`。如果某次正文特别长，超出了缓冲，会发生什么？

**参考答案**：`data_sz` 会被截断到 `buff_size`，即超出的正文尾部被丢弃，对端收到的正文是不完整的。这是一种「硬上限保护」——它不会崩溃，但可能让对端解析失败。正因如此，协议层面另有 `WEASEL_IPC_BUFFER_SIZE`（4 KiB / 2048 宽字符，见 u2-l1）来约束单次文本量，确保正常情况下不会触顶。注意通道自身的 `buff_size` 默认是 64 KiB（见 4.3 节构造函数），比协议文本上限宽裕。

**练习 3**：`_Send` 的 `catch(...)` 里先 `_Reconnect()` 再 `_WritePipe`。为什么不直接重试 `Transact`？

**参考答案**：因为连接可能已经损坏（对端关闭、管道破裂），不重连就直接写大概率还会失败。`_Reconnect` 会先 `_FinalizePipe` 关掉旧句柄、再 `_Ensure` 建立新连接；在新连接上重发一次，能覆盖「瞬时断开」这一最常见的可恢复场景。重连后仍失败则异常继续上抛，由 `_SendMessage` 的 `catch(DWORD)` 兜底返回 0。

---

### 4.3 线程本地句柄与缓冲流

#### 4.3.1 概念说明

前两节反复出现「线程本地句柄」「上下文缓冲」，本节把它们讲透。两个问题：

1. **为什么要线程本地？** 输入法场景里，同一段代码会被多个线程并发驱动（TSF 端的多线程回调、服务端的「每连接一线程」）。命名管道的「一条实例 = 一条顺序消息流」若被多线程交错读写，消息边界立刻错乱。给每个线程配**专属**句柄与缓冲，是最简单可靠的隔离方式。
2. **缓冲流是什么？** `boost::interprocess::wbufferstream` 是一个「在已有内存上构造」的宽字符流，不分配堆内存，`<<` 直接写进你给它的缓冲。Weasel 用它把 `L"session.client_app=..."` 这类文本**零拷贝**地写进管道发送缓冲。

#### 4.3.2 核心流程

线程本地资源的生命周期：

```
某线程首次访问 _GetPipeHandle():
    hpipe_ptr 为空 → new HANDLE(INVALID_HANDLE_VALUE) 并存入 TLS
    返回该指针
该线程首次访问 _GetContext():
    context 为空 → new ChannelContext(buff_size) 并存入 TLS
    ChannelContext 构造时分配 buff_size 字节的 buffer
该线程退出:
    boost::thread_specific_ptr 自动 delete 本线程的 HANDLE* 与 ChannelContext*
```

`ChannelContext` 把「缓冲 + 流 + 是否有正文」三者打包：

[include/PipeChannel.h:13-22](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L13-L22) —— `Stream` 是 `wbufferstream`；`ChannelContext` 持有 `buffer`（字节数组）、`write_stream`（延迟创建的流）、`has_body`（正文标志）。

把上节用到的两块拼起来看：

[include/PipeChannel.h:43-55](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L43-L55) —— `_GetPipeHandle()` 与 `_GetContext()` 都是「首次访问时惰性创建并放进 TLS」的访问器。

#### 4.3.3 源码精读

两个线程本地成员的声明（注意 `mutable`，因为访问器是 `const`）：

[include/PipeChannel.h:57-63](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L57-L63) —— `mutable boost::thread_specific_ptr<HANDLE> hpipe_ptr;` 与 `mutable boost::thread_specific_ptr<ChannelContext> context;`。`buff_size` 是 `const`，在构造时确定。

构造函数决定缓冲大小——这是 4.2 练习 2 里「64 KiB」的来源：

[include/PipeChannel.h:87-90](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L87-L90) —— 模板构造默认 `bs = 64 * 1024`。

[WeaselIPC/PipeChannel.cpp:18-21](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L18-L21) —— 基类构造签名里写的默认值是 `4 * 1024`，但子类 `PipeChannel` 默认 64 KiB 覆盖了它，所以实际通道缓冲是 64 KiB。

析构对 TLS 有一个重要说明：

[WeaselIPC/PipeChannel.cpp:23-25](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPC/PipeChannel.cpp#L23-L25) —— 注释明确：`thread_specific_ptr` 的清理是**自动**的。也就是说，每个线程退出时，boost 会自动释放该线程的 `HANDLE*` 与 `ChannelContext*`，对象析构无需手动遍历。

缓冲流尺寸的两个内联计算：

[include/PipeChannel.h:196-202](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/include/PipeChannel.h#L196-L202) —— `_SendBufferSizeW()` 与 `_ReceiveBufferSizeW()` 都按「(总容量 − 头大小) / sizeof(wchar_t)」算可用宽字符数，因为 `wchar_t` 是 2 字节。

服务端的线程模型把「每连接一线程」体现得最清楚：

[WeaselIPCServer/WeaselServerImpl.cpp:408-421](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L408-L421) —— `Listen` 死循环：每次 `_ConnectServerPipe` 等到一个客户端，就 `boost::thread` 派生一个新线程跑 `_ProcessPipeThread(pipe, handler)`。每个这样的线程都有自己的 TLS 句柄与上下文，互不干扰。

[WeaselIPCServer/WeaselServerImpl.cpp:178-180](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/WeaselIPCServer/WeaselServerImpl.cpp#L178-L180) —— 这个监听循环本身跑在 `ServerImpl::Run()` 起的 `boost::thread` 里，与 WTL 消息循环（`CMessageLoop`）分离开，避免阻塞 UI。

> `boost::thread_specific_ptr` 的语义补充：它给每个线程维护一份独立的指针，线程退出时按定制（或默认 `delete`）清理。Weasel 没有定制清理器，所以依赖 `HANDLE*` 与 `ChannelContext*` 的析构——前者是内置类型（无副作用），后者会释放 `buffer` 与 `write_stream`，资源不漏。

#### 4.3.4 代码实践

**实践目标**：用一个现成的端到端测试程序，观察「客户端线程 ↔ 独立管道实例 ↔ 服务端工作线程」的对应关系。

**操作步骤**（阅读 + 运行型）：

1. 打开测试工程入口 [test/TestWeaselIPC/TestWeaselIPC.cpp](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp)。
2. 阅读 `console_main`（[L67-99](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L67-L99)）：`client.Connect()` → `StartSession()` → 循环 `ProcessKeyEvent` → `GetResponseData(read_buffer)`。
3. 阅读 `server_main`（[L165-182](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L165-L182)）：`Server` + `TestRequestHandler`，`server.Run()` 进入消息循环与监听线程。
4. 构建并运行（参考 u1-l3 的构建方式）：先 `TestWeaselIPC.exe /start` 启动服务端，再 `TestWeaselIPC.exe`（无参数，走 `client_main`）或 `/console` 启动客户端。
5. 在客户端输入一个字符，观察服务端控制台打印的 `ProcessKeyEvent: ... keycode: ...`（来自 [L151-159](https://github.com/rime/weasel/blob/f9203cae5e2b0796d94575b975f62a6be9614b00/test/TestWeaselIPC/TestWeaselIPC.cpp#L151-L159) 的 `TestRequestHandler::ProcessKeyEvent`），以及客户端打印的 `server replies: 1` 与 `buffer reads: ...`。

**需要观察的现象 / 预期结果**：

- 客户端 `ProcessKeyEvent` 返回 `true`（`eaten==1`），随后 `GetResponseData` 读到的正文就是服务端 `eat(std::wstring(L"Greeting=Hello, 小狼毫.\n"))` 写下的那串文本。
- 这条文本经历的路径完全在本讲的范围内：服务端 `*channel << msg`（写正文区）→ `_Send(pipe, resp)`（拼上结果码 `DWORD` 一起发）→ 客户端 `_ReceiveResponse` 读回结果码 → `HandleResponseData` 把正文交给 `read_buffer` 回调。

> 待本地验证：上面的运行步骤需要在配置好 `BOOST_ROOT` 的 Windows + MSBuild/xmake 环境中执行（参见 u1-l3）。若暂无环境，可只做阅读部分，并对照 4.3.3 的源码链接理解线程归属。

#### 4.3.5 小练习与答案

**练习 1**：如果去掉 `hpipe_ptr` 的线程本地属性、改成所有线程共享一个 `HANDLE`，会出什么问题？

**参考答案**：多个线程会共用同一条管道实例，并发 `WriteFile`/`ReadFile` 会让「请求-响应」错配——A 线程发的请求可能被 B 线程的 `ReadFile` 读走，导致会话串台、消息内容错乱。即便加锁保证顺序，也会把原本可并行的多连接强行串成一条，退化性能。线程本地句柄让每个线程各走各的实例，天然隔离。

**练习 2**：`ChannelContext` 里的 `write_stream` 为什么是 `unique_ptr` 且延迟创建，而不是直接成员？

**参考答案**：延迟创建（`_BufferWriteStream` 里首次访问时才 `make_unique`）让「不写正文」的命令（如纯按键请求）完全不必构造流，省去一次 `memset` 与对象构造；用 `unique_ptr` 还能在 `ClearBufferStream` 里 `reset(nullptr)` 彻底释放，下次再用时重建，避免旧流残留位置信息污染下一次写入。

---

## 5. 综合实践

把三个最小模块串起来，画出**一次 `Transact(msg)` 的完整时序图**（这是本讲的核心实践任务）。

要求：在一张图里同时体现客户端、服务端、管道、线程本地存储与缓冲流，至少覆盖以下节点：

1. 客户端线程调用 `channel << 正文`（若需要正文）→ `_BufferWriteStream` 在 `buffer + _MsgSize` 上建流。
2. 客户端调用 `Transact(msg)`：`_Ensure()` →（必要时）`_Connect`/`_TryConnect` 建立**本线程**的管道实例。
3. `_Send`：把 `msg` 拷到缓冲开头，用 `tellp` 算正文字节数，`_WritePipe` 一次写出 `[头][正文]`，`FlushFileBuffers`，最后 `ClearBufferStream`。
4. 服务端 `Listen` 的某次 `_ConnectServerPipe` 命中该连接，派生 `_ProcessPipeThread`（**独立工作线程**，拥有自己的 TLS 上下文）。
5. 服务端 `_Receive(pipe, &msg, sizeof(msg))` 读到 `PipeMessage`；若带正文，触发 `ERROR_MORE_DATA` 分支读入正文区。
6. 服务端 `HandlePipeMessage` 派发到具体 `OnXxx`，期间通过 `eat` 回调 `*channel << 响应正文`。
7. 服务端 `resp(result)` → `_Send(pipe, DWORD)`，把 `[结果码][响应正文]` 一次发回。
8. 客户端 `_ReceiveResponse` 读回 `DWORD`；必要时 `ERROR_MORE_DATA` 把响应正文读入上下文缓冲。
9. 客户端 `HandleResponseData(handler)` 把正文交给业务回调。

**作图建议**：

- 用两条竖直泳道分别表示「客户端线程」和「服务端工作线程」，中间画一个管道图标。
- 用不同颜色标注：缓冲区操作（`<<`、`tellp`、`ClearBufferStream`）、TLS 访问（`_GetPipeHandle`/`_GetContext`）、系统调用（`CreateFile`/`WriteFile`/`ReadFile`/`FlushFileBuffers`）。
- 在 `_Send` 与 `_Receive` 旁注明「`ERROR_MORE_DATA` 时读正文」的分支。

**自检清单**（画完后逐条对照）：

- [ ] 图里是否标出了客户端用线程本地句柄、服务端用显式 `pipe` 参数的不对称？
- [ ] 是否标出了正文在客户端「`Transact` 之前用 `<<` 累积」、在服务端「`resp(result)` 之前用 `eat` 累积」？
- [ ] 是否标出了 `_Send` 写失败时的「重连重发一次」分支？
- [ ] 是否标出了每个服务端连接独占一个 `_ProcessPipeThread`、各自一份 TLS 上下文？

> 这是一个**源码阅读 + 文档产出型**实践，不需要运行环境即可完成；产出可作为你后续阅读 u2-l3（Client/Server 实现）时的导航图。

---

## 6. 本讲小结

- `PipeChannelBase` 把命名管道的「按需连接、忙等实例、消息模式读写」封装成与消息类型无关的原语，并用 `DWORD` 异常码（`_ThrowLastError`/`_ThrowIfNot`）向上传递错误，由客户端 `catch(DWORD)` 兜底。
- `_Connect` 的循环依赖一个关键约定：`ERROR_PIPE_BUSY` 代表「实例忙、可重试」，其它错误码（如服务未启动时的 `ERROR_FILE_NOT_FOUND`）会抛出，被 `_Ensure` 吞为 `return false`，驱动上层启动服务。
- `PipeChannel` 模板用 `_TyMsg`/`_TyRes` 站在**本端视角**描述「我发什么 / 我收什么」，所以客户端写 `PipeChannel<PipeMessage>`、服务端写 `PipeChannel<DWORD, PipeMessage>`，参数顺序相反。
- `Transact(msg)` = `_Ensure` + `_Send` + `_ReceiveResponse`；正文在 `Transact` 之前用 `operator<<` 累积进 `wbufferstream`，`_Send` 用 `tellp` 量出正文字节数，与定长头一次性写出，写失败会重连重发一次。
- `_Receive` 利用消息模式管道的 `ERROR_MORE_DATA` 把「超长正文」分两次读：先读定长头，再把正文读进上下文缓冲；响应正文随后由 `HandleResponseData` 交给业务回调。
- 线程隔离靠 `boost::thread_specific_ptr`：每个线程一份管道句柄（`hpipe_ptr`）和一份 `ChannelContext`（`buffer` + `write_stream` + `has_body`），线程退出时自动清理；服务端为每个连接派生独立 `_ProcessPipeThread`，天然适配这一模型。

---

## 7. 下一步学习建议

本讲把「通道」讲透了，但还没有讲「谁在调用通道」。建议接下来：

1. **u2-l3 IPC 客户端与服务器实现**：精读 `WeaselClientImpl`（`Connect` 如何决定启动 Server、`StartSession`/`ProcessKeyEvent` 如何串起 `<<` 与 `Transact`）与 `WeaselServerImpl`/`PipeServer`（`Listen` 的线程模型、`HandlePipeMessage` 的命令派发、托盘命令），把本讲的通道调用补全成完整业务链路。
2. **u2-l4 数据模型与 boost 序列化**：本讲的正文只是「文本」，但 `Context`/`Status`/`UIStyle` 这些结构化数据如何序列化进这段文本？这涉及 `WeaselIPCData.h` 与 boost 序列化，是理解正文内容的下一站。
3. **u2-l5 响应解析、反序列化与动作分发**：客户端拿到正文后如何把它「解释」成 UI 更新？`ResponseParser`/`Deserializer`/`ActionLoader` 会回答这个问题，与本讲的 `HandleResponseData` 正好衔接。

如果你想立刻动手验证本讲内容，推荐先用 u1-l3 的方式构建 `test/TestWeaselIPC`，再按 4.3.4 的步骤跑一遍 `/start` + `/console`，在真实管道上感受一次 `Transact`。
