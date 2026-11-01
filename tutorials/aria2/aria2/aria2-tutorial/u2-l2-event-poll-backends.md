# u2-l2 EventPoll：I/O 多路复用的多后端抽象

## 1. 本讲目标

上一讲（u2-l1）我们看到 `DownloadEngine::run()` 每圈循环的第一步是 `waitData()`，它把控制权交给一个 `EventPoll` 对象去等待网络事件。本讲就钻进这个「等待」的内部：

1. 说清 `EventPoll` 抽象接口有哪些方法、统一的事件位标志长什么样。
2. 对比 epoll / kqueue / port / select 四个平台后端在「事件结构体、注册/注销接口、等待调用」上的异同。
3. 解释抽象事件常量（`EventPoll::EVENT_READ` 等）如何映射到各平台原语（`EPOLLIN`、`EVFILT_READ`、`POLLIN`、`fd_set`）。
4. 理解 `SocketEntry`/`CommandEvent` 回调骨架，以及「事件回调只置位、命令延后到 `commands_` 队列执行」的机制。

> 一个诚实的更正：本手册大纲里把统一事件常量写作 `A2_EVENT_NET_*`，但在当前源码树中并不存在这个名字，实际的名字是 `EventPoll::EventType` 枚举里的 `EVENT_READ`/`EVENT_WRITE`/`EVENT_ERROR`/`EVENT_HUP`。本讲按真实代码讲解。

## 2. 前置知识

### 2.1 为什么需要 I/O 多路复用

aria2 单线程就要同时照看成百上千个 socket：几十条 HTTP 连接、几十个 BT peer、DHT 的 UDP socket、RPC 监听 socket……如果每个 socket 都开一个线程阻塞读，线程数会爆炸。**I/O 多路复用（I/O multiplexing）** 的思路是：把所有关心的 socket 描述符（fd）打包交给操作系统内核，「任何一个 fd 上有我关心的事（可读/可写/出错）就叫醒我，并告诉我是谁」。这正是上一讲说的 Reactor 模式里的「事件分离器（demultiplexer）」角色。

### 2.2 四种平台原语一览

| 原语 | 平台 | 核心数据结构 | 注册方式 | 就绪返回 |
|---|---|---|---|---|
| `select` | 所有 POSIX / Winsock | `fd_set` 位图 | 每次调用前全量重建 `fd_set` | 返回后线性扫描 `FD_ISSET` |
| `epoll` | Linux | 内核红黑树 + 就绪链表 | `epoll_ctl` 增量注册一次 | `epoll_wait` 只返回就绪项 |
| `kqueue` | BSD / macOS | `kevent` 数组 | `EV_SET` + `kevent` 提交变更 | `kevent` 只返回就绪项 |
| event port | Solaris / illumos | 事件端口 | `port_associate`（**一次性**，触发即失效） | `port_getn` 返回后需重新关联 |

补充两点背景：

- `select` 的 `fd_set` 是定长位图，fd 值不能超过 `FD_SETSIZE`（通常 1024），且每次调用都要把整个位图从用户态拷进内核、返回后再 O(n) 扫描；epoll/kqueue 则把「注册」和「等待」拆开，等待时只拿就绪列表，复杂度近似 O(就绪数)。
- kqueue 的模型与其他三家不同：它不是「一坨事件位」，而是**过滤器（filter）**——同一个 fd 的读兴趣和写兴趣是两条独立的 `kevent`（`EVFILT_READ` / `EVFILT_WRITE`），后面源码里会看到这个差异如何体现。

### 2.3 位标志（bitmask）

aria2 用一个 `int` 的不同二进制位表示不同事件：`EVENT_READ = 1`（二进制 `0001`）、`EVENT_WRITE = 1 << 1`（`0010`）。判断「事件 e 里有没有读」用按位与 `EVENT_READ & e`，合并用按位或。这样一次注册/通知可以同时携带多种事件。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/EventPoll.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EventPoll.h) | 抽象接口：4 个事件位 + `poll`/`addEvents`/`deleteEvents` 纯虚函数 |
| [src/Event.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h) | 与后端无关的回调骨架模板：`SocketEntry`、`CommandEvent`、`ADNSEvent` |
| [src/EpollEventPoll.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc) / [.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.h) | Linux epoll 后端 |
| [src/KqueueEventPoll.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/KqueueEventPoll.cc) / [.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/KqueueEventPoll.h) | BSD/macOS kqueue 后端 |
| [src/PortEventPoll.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PortEventPoll.cc) / [.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PortEventPoll.h) | Solaris/illumos event port 后端 |
| [src/SelectEventPoll.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc) | 兜底 select 后端（唯一无编译宏门槛、必被编译的一个） |
| [src/DownloadEngineFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc) | 按 `--event-poll` 选项 + 编译宏选择后端 |
| [src/DownloadEngine.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc) | 调用方：`waitData`、读写检查的注册包装 |
| [src/Command.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.h) / [src/Command.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.cc) | 事件标志位（`readEvent_` 等）与状态迁移的载体 |

另有两个同族后端本讲不展开：`PollEventPoll`（POSIX `poll(2)`，见 [src/PollEventPoll.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PollEventPoll.h)）和 `LibuvEventPoll`（libuv，主要服务 Windows，见 [src/LibuvEventPoll.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibuvEventPoll.h)），它们的骨架与四后端一致。

## 4. 核心概念与源码讲解

### 4.1 EventPoll 接口：五个方法与四个事件位

#### 4.1.1 概念说明

`EventPoll` 是纯抽象基类，定义「事件分离器」必须会做的事。上层（`DownloadEngine`）只认识这个接口，完全不知道底层是 epoll 还是 select——这就是「多后端抽象」：换一个操作系统，只换实现类，调用代码一行不改。

#### 4.1.2 核心流程

接口语义可以概括为一句话：**「谁（socket）上的什么事件（EventType）归谁处理（Command*）」的登记簿，外加一个限时等待方法**。

```text
addEvents(fd, cmd, EVENT_READ)     # 登记：fd 可读时唤醒 cmd
addEvents(fd, cmd, EVENT_WRITE)    # 登记：fd 可写时唤醒 cmd
poll(tv)                           # 最多等 tv 时长；期间发生的事件
                                   #   → 转成对 Command 的「置位」调用
deleteEvents(fd, cmd, EVENT_READ)  # 注销某个登记
addNameResolver / deleteNameResolver  # 异步 DNS（c-ares）专用的登记入口
```

#### 4.1.3 源码精读

统一事件位定义在枚举里，四个位互不重叠，可自由按位或组合：

[EventPoll.h:L54-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EventPoll.h#L54-L59)
```cpp
enum EventType {
  EVENT_READ = 1,
  EVENT_WRITE = 1 << 1,
  EVENT_ERROR = 1 << 2,
  EVENT_HUP = 1 << 3,
};
```
这段定义了抽象层自己的「通用语言」：读、写、错误、挂断（hangup，对端关闭连接）。

三个纯虚方法是每个后端必须实现的全部契约：

[EventPoll.h:L63-L68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EventPoll.h#L63-L68)
```cpp
virtual void poll(const struct timeval& tv) = 0;

virtual bool addEvents(sock_t socket, Command* command, EventType events) = 0;

virtual bool deleteEvents(sock_t socket, Command* command,
                          EventType events) = 0;
```
`poll(tv)` 等待至多 `tv` 时长的事件并分发；`addEvents`/`deleteEvents` 增删「fd + Command + 事件位」的登记，返回 `bool` 表示是否成功。

若编译时启用了异步 DNS（c-ares），还有两个额外入口，把 `AsyncNameResolver` 内部的 socket 也纳入同一套等待机制：

[EventPoll.h:L69-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EventPoll.h#L69-L77)
（`addNameResolver` / `deleteNameResolver`，受 `#ifdef ENABLE_ASYNC_DNS` 保护。）

#### 4.1.4 代码实践

1. **实践目标**：确认你手头这个 aria2 二进制支持哪些事件后端。
2. **操作步骤**：运行 `./src/aria2c --help | grep -A2 event-poll`；再运行 `./src/aria2c --version | head -5` 看特性汇总。
3. **观察现象**：帮助文本中 `--event-poll` 后列出的可选值（Linux 上通常是 `epoll, poll, select`）。
4. **预期结果**：可选值集合 = 编译期探测到的宏（`HAVE_EPOLL` 等）加 `select`。若强制写 `--event-poll=kqueue`（Linux 上）会报「unrecognized option」类错误，因为合法值列表在 [OptionHandlerFactory.cc:L284-L310](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L284-L310) 里按宏条件生成。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EVENT_ERROR`/`EVENT_HUP` 几乎从不单独出现在 `addEvents` 的注册参数里？
**答案**：错误和挂断不是「你请求监听」的兴趣，而是内核**总会报告**的被动状态。注册时一般只给 `EVENT_READ`/`EVENT_WRITE`；这两个位主要用在「分发阶段」——事件回调看到它们就额外调用 `errorEventReceived()`/`hupEventReceived()`（见 4.2.3）。

**练习 2**：`poll(const struct timeval& tv)` 的返回值是 `void`，那「发生了哪些事件」的信息去哪了？
**答案**：通过副作用传递。`poll` 内部直接调用事件的 `processEvents`，进而调用 `Command::setStatusActive()`、`readEventReceived()` 等方法，把结果「写」在 Command 对象的状态位上，等 `DownloadEngine::executeCommand` 稍后消费。这就是 4.5 要讲的延迟执行。

### 4.2 事件回调骨架：SocketEntry 与 CommandEvent

#### 4.2.1 概念说明

四个后端有大量共性逻辑：「按 fd 聚合一堆 (Command, 事件位) 订阅」以及「把内核报告的事件位分发给订阅者」。aria2 把这部分共性抽成 [src/Event.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h) 里的模板类，后端只需把「聚合结果」翻译成自家的注册结构体。三个角色：

- **`SocketEntry`**：一个 fd 上的订阅表（`fd → deque<(Command, events)>`）。
- **`CommandEvent`**：一条订阅记录——「Command 关心这个 fd 上的哪些事件位」，以及收到事件时该怎么通知 Command。
- **`ADNSEvent`**：c-ares socket 的订阅记录，通知方式是驱动 resolver 处理数据而非直接读 socket。

#### 4.2.2 核心流程

事件到达后的分发路径（对所有后端相同）：

```text
后端 poll() 拿到 (fd, 就绪事件位 events)
  → 找到该 fd 的 SocketEntry
  → SocketEntry::processEvents(events)
      → 对每条 CommandEvent 订阅：
          if (订阅位 & 就绪位) 或 (就绪位含 ERROR/HUP):
              command->setStatusActive()        # 唤醒：把它标记为待执行
          if 就绪位含 READ:  command->readEventReceived()   # 记账
          if 就绪位含 WRITE: command->writeEventReceived()
          if 就绪位含 ERROR: command->errorEventReceived()
          if 就绪位含 HUP:   command->hupEventReceived()
```

注意：**全程没有任何 `execute()` 调用、没有网络读写**。回调只做两件事——置「待执行」状态、记「发生过什么事件」。真正的执行要等主循环回到 `executeCommand`（见 4.5）。

#### 4.2.3 源码精读

订阅记录与它的通知逻辑（模板参数 `EventPoll` 提供该后端的事件位常量 `IEV_*`）：

[Event.h:L93-L111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L93-L111)
```cpp
virtual void processEvents(int events)
{
  if ((events_ & events) ||
      ((EventPoll::IEV_ERROR | EventPoll::IEV_HUP) & events)) {
    command_->setStatusActive();
  }
  if (EventPoll::IEV_READ & events) {
    command_->readEventReceived();
  }
  if (EventPoll::IEV_WRITE & events) {
    command_->writeEventReceived();
  }
  ...
}
```
这段是回调核心：只要「我订阅的事件发生了」或「出了错误/挂断」，就把命令标成 ACTIVE（可被调度）；随后按就绪位给命令打上对应的 IO 标志。

订阅表本体是个模板容器，持有 fd 和两条订阅队列：

[Event.h:L185-L195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L185-L195)
```cpp
template <typename CommandEvent, typename ADNSEvent> class SocketEntry {
protected:
  sock_t socket_;

  std::deque<CommandEvent> commandEvents_;
#ifdef ENABLE_ASYNC_DNS
  std::deque<ADNSEvent> adnsEvents_;
#endif
```

同一命令重复登记时做**按位合并**而不是重复插入：

[Event.h:L212-L222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L212-L222)
```cpp
void addCommandEvent(const CommandEvent& cev)
{
  auto i = std::find(commandEvents_.begin(), commandEvents_.end(), cev);
  if (i == commandEvents_.end()) {
    commandEvents_.push_back(cev);
  }
  else {
    (*i).addEvents(cev.getEvents());   // 已有该命令的订阅 → 位或合并
  }
}
```
`CommandEvent::operator==` 只比较 `command_` 指针（[Event.h:L86-L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L86-L89)），所以「同一命令先注册读、再注册写」最终合成一条订阅、事件位为 `READ|WRITE`。

分发入口遍历两条队列：

[Event.h:L276-L285](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L276-L285)
（`processEvents(int events)` 用 `std::for_each` 依次调用每条 `CommandEvent::processEvents` 和 `ADNSEvent::processEvents`。）

c-ares 订阅记录的通知方式对比：它不碰 Command 的 IO 标志，而是把就绪的 fd 喂给 resolver，然后同样 `setStatusActive()`：

[Event.h:L148-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L148-L168)
```cpp
virtual void processEvents(int events)
{
  ares_socket_t readfd;
  ares_socket_t writefd;
  ...
  resolver_->process(readfd, writefd);
  command_->setStatusActive();
}
```

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「同一 Command 的读写订阅会合并」。
2. **操作步骤**：通读 [Event.h:L66-L122](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L66-L122) 中 `CommandEvent` 的 `addEvents`/`removeEvents`/`eventsEmpty` 三个方法（各只有一行）。
3. **观察现象**：`addEvents` 是 `events_ |= events`，`removeEvents` 是 `events_ &= (~events)`。
4. **预期结果**：你能回答——若一条订阅的位被逐步减到 0（`eventsEmpty()` 为真），`SocketEntry::removeCommandEvent`（[Event.h:L224-L237](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L224-L237)）会把这条记录从 deque 里 `erase` 掉，fd 上再无任何订阅时整个 `SocketEntry` 也会被后端删除（见 4.3.3 的 `deleteEvents`）。

#### 4.2.5 小练习与答案

**练习 1**：`SocketEntry` 为什么用 `std::deque` 而不是 `std::map<Command*, int>`？
**答案**：这是纯实现取舍。订阅数量通常很小（一个 fd 上一般只有一两个命令），线性 `std::find` 的 deque 更简单、缓存友好，也避免了为 Command 定义全序。代价是查找 O(n)，但 n 极小。

**练习 2**：`processEvents` 里为什么 `ERROR`/`HUP` 不检查 `events_ & events` 就直接 `setStatusActive()`？
**答案**：因为错误与挂断影响所有订阅者——即使某命令只订阅了 WRITE，连接断开也必须唤醒它去处理失败（如重试、换镜像），否则它会永远睡眠。源码注释体现在那行 `((EventPoll::IEV_ERROR | EventPoll::IEV_HUP) & events)` 的无条件或判断上。

### 4.3 epoll 后端：EpollEventPoll 逐行精读

#### 4.3.1 概念说明

Linux 上默认首选的后端。epoll 的关键特性是「注册与等待分离」：`epoll_ctl` 把 fd 和兴趣事件挂到内核实例上（持久有效），`epoll_wait` 只负责拿就绪列表。于是 aria2 需要维护一份用户态镜像 `socketEntries_`（`std::map<sock_t, KSocketEntry>`），保证「内核里的注册状态」与「用户态的订阅表」始终一致。

#### 4.3.2 核心流程

```text
构造:  epoll_create(1024) → epfd
注册:  addEvents(fd, cmd, EVENT_*)
        → translateEvents: EVENT_READ→EPOLLIN, EVENT_WRITE→EPOLLOUT,
                           EVENT_ERROR→EPOLLERR, EVENT_HUP→EPOLLHUP
        → 在 socketEntries_ 里找到/新建 KSocketEntry
        → 订阅并入表 (event.addSelf)
        → KSocketEntry::getEvents() 把所有订阅位聚合进一个 epoll_event
        → epoll_ctl(EPOLL_CTL_MOD 已有 | EPOLL_CTL_ADD 新来)
等待:  poll(tv)
        → timeout 毫秒 = tv 秒*1000 + tv 微秒/1000
        → epoll_wait 循环直到非 EINTR
        → 每个就绪项: data.ptr 就是 KSocketEntry* → processEvents(就绪位)
注销:  deleteEvents → 从订阅表移除位; 位全空则 epoll_ctl(EPOLL_CTL_DEL) 并删除 entry,
        否则 EPOLL_CTL_MOD 更新聚合位
```

#### 4.3.3 源码精读

构造即创建 epoll 实例，并预分配 1024 个结果槽位：

[EpollEventPoll.cc:L84-L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L84-L89)
```cpp
EpollEventPoll::EpollEventPoll()
    : epEventsSize_(EPOLL_EVENTS_MAX),
      epEvents_(make_unique<struct epoll_event[]>(epEventsSize_))
{
  epfd_ = epoll_create(EPOLL_EVENTS_MAX);
}
```
`EPOLL_EVENTS_MAX` 为 1024（[EpollEventPoll.h:L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.h#L90)）。若 `epfd_ == -1`，`good()` 返回假，工厂会直接抛异常（见 4.5.3）。

`poll` 的三段式——等待、分发、c-ares 补拍：

[EpollEventPoll.cc:L106-L122](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L106-L122)
```cpp
void EpollEventPoll::poll(const struct timeval& tv)
{
  // timeout is millisec
  int timeout = tv.tv_sec * 1000 + tv.tv_usec / 1000;

  int res;
  while ((res = epoll_wait(epfd_, epEvents_.get(), EPOLL_EVENTS_MAX,
                           timeout)) == -1 &&
         errno == EINTR)
    ;

  if (res > 0) {
    for (int i = 0; i < res; ++i) {
      KSocketEntry* p = reinterpret_cast<KSocketEntry*>(epEvents_[i].data.ptr);
      p->processEvents(epEvents_[i].events);
    }
  }
```
三个细节值得圈出：`timeval` 换算成毫秒；`EINTR`（被信号打断）时原地重试，且此时 timeout 不重置——宁可早醒不可久睡；注册时把 `epEvent.data.ptr` 填成 `KSocketEntry*` 自己（见下），就绪时零查找成本直接分发。

抽象事件位 → Linux 原语的翻译表：

[EpollEventPoll.cc:L145-L163](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L145-L163)
```cpp
int translateEvents(EventPoll::EventType events)
{
  int newEvents = 0;
  if (EventPoll::EVENT_READ & events) {
    newEvents |= EPOLLIN;
  }
  if (EventPoll::EVENT_WRITE & events) {
    newEvents |= EPOLLOUT;
  }
  if (EventPoll::EVENT_ERROR & events) {
    newEvents |= EPOLLERR;
  }
  if (EventPoll::EVENT_HUP & events) {
    newEvents |= EPOLLHUP;
  }
  return newEvents;
}
```
而反向翻译（内核报告的 `EPOLLIN` → 回调里的 `IEV_READ`）靠的是每个后端类上的常量表：

[EpollEventPoll.h:L126-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.h#L126-L129)
```cpp
static const int IEV_READ = EPOLLIN;
static const int IEV_WRITE = EPOLLOUT;
static const int IEV_ERROR = EPOLLERR;
static const int IEV_HUP = EPOLLHUP;
```
因为 epoll 报告的事件位本身就是 `EPOLLIN` 这些值，`IEV_*` 直接取同值，`CommandEvent::processEvents`（4.2.3）里 `EventPoll::IEV_READ & events` 的判断天然成立。这是「模板参数 EventPoll 提供 IEV_* 常量」设计的关键：**让每个后端自己声明「我的就绪位长什么样」**。

聚合与注册——把订阅表压缩成一个 `epoll_event`：

[EpollEventPoll.cc:L61-L82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L61-L82)
```cpp
struct epoll_event EpollEventPoll::KSocketEntry::getEvents()
{
  struct epoll_event epEvent;
  memset(&epEvent, 0, sizeof(struct epoll_event));
  epEvent.data.ptr = this;
  epEvent.events = std::accumulate(commandEvents_.begin(), commandEvents_.end(),
                                   0, accumulateEvent);
  return epEvent;
}
```
`std::accumulate` + `accumulateEvent`（按位或）把该 fd 上**所有命令、所有 c-ares 订阅**的兴趣位并成一个整数——epoll 只认「这个 fd 的总兴趣」，不关心是谁订阅的；订阅明细留在用户态 `SocketEntry` 里，就绪时再细分发。

注册路径上有一处精彩的防御性补救——`MOD` 失败回退 `ADD`：

[EpollEventPoll.cc:L171-L199](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L171-L199)
```cpp
  if (i != std::end(socketEntries_) && (*i).first == socket) {
    ...
    r = epoll_ctl(epfd_, EPOLL_CTL_MOD, socketEntry.getSocket(), &epEvent);
    if (r == -1) {
      // try EPOLL_CTL_ADD: There is a chance that previously socket X is
      // added to epoll, but it is closed and is not yet removed from
      // SocketEntries. In this case, EPOLL_CTL_MOD is failed with ENOENT.
      r = epoll_ctl(epfd_, EPOLL_CTL_ADD, socketEntry.getSocket(), &epEvent);
```
（新 fd 分支在 [L187-L199](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L187-L199)，其中 L190-L193 处当登记数超过结果槽位数时把 `epEvents_` 数组容量翻倍。）

注销路径的两种结局：

[EpollEventPoll.cc:L239-L257](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L239-L257)
```cpp
  if (socketEntry.eventEmpty()) {
    // In kernel before 2.6.9, epoll_ctl with EPOLL_CTL_DEL requires non-null
    // pointer of epoll_event.
    struct epoll_event ev = {0, {0}};
    r = epoll_ctl(epfd_, EPOLL_CTL_DEL, socketEntry.getSocket(), &ev);
    socketEntries_.erase(i);
  }
  else {
    struct epoll_event epEvent = socketEntry.getEvents();
    r = epoll_ctl(epfd_, EPOLL_CTL_MOD, socketEntry.getSocket(), &epEvent);
```
订阅清空 → 从内核摘除 fd 并删除用户态 entry；还有剩余订阅 → 用缩减后的聚合位 `MOD` 更新。那条 2.6.9 前内核的注释是好教材：`EPOLL_CTL_DEL` 在老内核要求非空指针，所以传了个栈上零值。

最后是 c-ares 的「每轮重挂」补拍（启用异步 DNS 时）：

[EpollEventPoll.cc:L128-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L128-L139)
```cpp
  for (auto& i : nameResolverEntries_) {
    auto& ent = i.second;
    ent.processTimeout();
    ent.removeSocketEvents(this);
    ent.addSocketEvents(this);
  }
```
注释解释了原因：c-ares 可能在自己的 API 里创建/关闭 socket，无法增量维护，干脆每轮「全拆再全挂」。`AsyncNameResolverEntry::addSocketEvents` 的实现见 [Event.h:L320-L337](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L320-L337)。

#### 4.3.4 代码实践（观察型）

1. **实践目标**：亲眼看到 `poll` 的错误分支日志。
2. **操作步骤**：`./src/aria2c --log=epoll.log --log-level=info -d /tmp <某个 URL>`，下载完成后 `grep -n "epoll_wait\|Failed to" epoll.log`。
3. **观察现象**：正常下载时这些关键词基本不出现（`addEvents` 失败走的是 `A2_LOG_DEBUG`，需要 `--log-level=debug` 才可见）。
4. **预期结果**：把 `--log-level` 换成 `debug` 后，若某个 fd 已被对端关闭，可能看到 `Failed to delete socket event, but may be ignored` 一类行，正对应 [EpollEventPoll.cc:L248-L256](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L248-L256) 的容错分支。待本地验证（是否出现取决于服务器关闭连接的时机）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `KSocketEntry::getEvents()` 要把 c-ares 订阅位也聚合进 `epEvent.events`？
**答案**：内核里一个 fd 只有一条 epoll 注册记录。c-ares 的 socket 和普通命令的 socket 共用同一套登记机制，若不聚合，后注册的一方会把先注册一方的兴趣位覆盖掉，事件就会丢失。

**练习 2**：`epEvents_` 结果数组的容量为什么要随登记数增长（[L190-L193](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L190-L193)）？
**答案**：`epoll_wait` 一次最多往数组里写 `epEventsSize_` 个就绪事件。登记的 fd 数若超过数组容量，理论上一个瞬间所有 fd 同时就绪就会有事件装不下（本轮丢失；好在 level-triggered 模式下下轮会再报，但浪费时间片）。登记数超限时翻倍是廉价的保险。

### 4.4 kqueue / port / select 三兄弟：同形不同「神」

#### 4.4.1 概念说明

三个后端与 epoll 实现**同一个接口、同一套骨架**，差异集中在两点：①「注册」用什么系统调用、是否持久；②「就绪事件」用什么结构体承载、如何还原成抽象事件位。逐一来看。

#### 4.4.2 核心流程与源码精读

**kqueue（BSD/macOS）：过滤器模型。**

注册时每个 fd 要提交**两条** kevent，分别对应读过滤器和写过滤器，用 `EV_ENABLE`/`EV_DISABLE` 表达当前兴趣：

[KqueueEventPoll.cc:L79-L87](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/KqueueEventPoll.cc#L79-L87)
```cpp
  EV_SET(&eventlist[0], socket_, EVFILT_READ,
         EV_ADD |
             ((events & KqueueEventPoll::IEV_READ) ? EV_ENABLE : EV_DISABLE),
         0, 0, PTR_TO_UDATA(this));
  EV_SET(&eventlist[1], socket_, EVFILT_WRITE,
         EV_ADD |
             ((events & KqueueEventPoll::IEV_WRITE) ? EV_ENABLE : EV_DISABLE),
         0, 0, PTR_TO_UDATA(this));
```
与 epoll「一个 fd 一条注册、事件位取并集」不同，kqueue 是「一个 fd 两条过滤器、各自开关」。用户指针 `udata` 同样回指 `KSocketEntry`，注意 [L48-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/KqueueEventPoll.cc#L48-L52) 有个宏 `PTR_TO_UDATA` 兼容某些平台上 `udata` 字段类型不同的老系统。

就绪后要靠 `filter` 字段**反推**事件位：

[KqueueEventPoll.cc:L120-L132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/KqueueEventPoll.cc#L120-L132)
```cpp
      KSocketEntry* p = reinterpret_cast<KSocketEntry*>(kqEvents_[i].udata);
      int events = 0;
      int filter = kqEvents_[i].filter;
      if (filter == EVFILT_READ) {
        events = KqueueEventPoll::IEV_READ;
      }
      else if (filter == EVFILT_WRITE) {
        events = KqueueEventPoll::IEV_WRITE;
      }
      p->processEvents(events);
```
它的 `IEV_*` 借用 poll 的常量（[KqueueEventPoll.h:L131-L134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/KqueueEventPoll.h#L131-L134)：`IEV_READ = POLLIN` 等），且 `translateEvents`（[L155-L167](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/KqueueEventPoll.cc#L155-L167)）只翻译 READ/WRITE 两位——kqueue 不会像 epoll 那样在就绪事件里报 `POLLERR`/`POLLHUP`，aria2 选择了不映射。

注册与等待合用一个调用：`kevent(kqfd_, changelist, n, changelist, 0, &zeroTimeout)`（[L192](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/KqueueEventPoll.cc#L192)）——前半是提交变更（注册），后半是零超时取事件。

**event port（Solaris/illumos）：一次性注册。**

`port_associate` 注册的兴趣**触发一次即失效**（one-shot），所以分发循环里必须立刻重新关联：

[PortEventPoll.cc:L113-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PortEventPoll.cc#L113-L125)
```cpp
    for (uint_t i = 0; i < nget; ++i) {
      const port_event_t& pev = portEvents_[i];
      KSocketEntry* p = reinterpret_cast<KSocketEntry*>(pev.portev_user);
      p->processEvents(pev.portev_events);
      int r = port_associate(port_, PORT_SOURCE_FD, pev.portev_object,
                             p->getEvents().events, p);
```
先分发、随后立刻重新 `port_associate`——这是与 epoll 语义差最大的地方。就绪事件直接复用 poll 风格的位（[PortEventPoll.h:L132-L135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PortEventPoll.h#L132-L135) `IEV_READ = POLLIN` 等），`portev_events` 可原样传给 `processEvents`；用户指针放在 `portev_user`。另外 `port_getn` 有个著名怪癖，源码注释专门记了一笔：

[PortEventPoll.cc:L104-L111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PortEventPoll.cc#L104-L111)
```cpp
  // If port_getn was interrupted by signal, it can consume events but
  // not update nget!. For this very annoying bug, we have to check
  // actually event is filled or not.
  portEvents_[0].portev_user = (void*)-1;
  res = port_getn(port_, portEvents_, portEventsSize_, &nget, &timeout);
```
先给 `portev_user` 打哨兵值 `(void*)-1`，返回后再检查它有没有被真实事件覆盖，以此区分「真没事件」和「被信号打断但事件已装填」。这类平台怪癖全部被关在后端实现内部，抽象层毫发无伤——正是这套设计的价值。

**select（万能兜底）：每次重建位图。**

select 后端没有编译宏门槛（`DownloadEngineFactory.cc` 无条件 include），任何平台都能跑，但代价最大。它**不用** `IEV_*` 常量，`CommandEvent::processEvents` 直接拿抽象层的 `EventPoll::EVENT_*` 当就绪位：

[SelectEventPoll.cc:L58-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc#L58-L76)
```cpp
void SelectEventPoll::CommandEvent::processEvents(int events)
{
  if ((events_ & events) ||
      ((EventPoll::EVENT_ERROR | EventPoll::EVENT_HUP) & events)) {
    command_->setStatusActive();
  }
  if (EventPoll::EVENT_READ & events) {
    command_->readEventReceived();
  }
  ...
```
因为 select 的就绪信息本来就是 aria2 自己用 `FD_ISSET` 翻译出来的（见下），直接用抽象位最省事——这也解释了为什么 `SelectEventPoll` 在 `.cc` 里自带一套非模板的 `SocketEntry`/`CommandEvent`（[L53-L122](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc#L53-L122)），而不是复用 `Event.h` 的模板：模板依赖 `EventPoll::IEV_*` 常量，而它没有。

等待时先拷出两份 fd_set（select 会破坏性修改传入的位图，必须留底稿），然后全量扫描：

[SelectEventPoll.cc:L170-L205](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc#L170-L205)
```cpp
void SelectEventPoll::poll(const struct timeval& tv)
{
  fd_set rfds;
  fd_set wfds;

  memcpy(&rfds, &rfdset_, sizeof(fd_set));
  memcpy(&wfds, &wfdset_, sizeof(fd_set));
  ...
  do {
    struct timeval ttv = tv;
    retval = select(fdmax_ + 1, &rfds, &wfds, nullptr, &ttv);
  } while (retval == -1 && errno == EINTR);
```

返回后对**每个已登记 fd** 逐一 `FD_ISSET`，拼出抽象事件位再分发：

[SelectEventPoll.cc:L206-L222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc#L206-L222)
```cpp
  if (retval > 0) {
    for (auto& i : socketEntries_) {
      auto& e = i.second;
      int events = 0;
      if (FD_ISSET(e.getSocket(), &rfds)) {
        events |= EventPoll::EVENT_READ;
      }
      if (FD_ISSET(e.getSocket(), &wfds)) {
        events |= EventPoll::EVENT_WRITE;
      }
      ...
      e.processEvents(events);
```

由于 select 的 fd_set 是调用时临时拼的，任何增删登记都要全量重算底稿——`addEvents`/`deleteEvents` 的最后一行都是 `updateFdSet()`（[L291-L322](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc#L291-L322)），重算逻辑在 [L250-L289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc#L250-L289)，其中藏着 select 的天花板警告：

[SelectEventPoll.cc:L265-L271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc#L265-L271)
```cpp
    if (fd < 0 || FD_SETSIZE <= fd) {
      A2_LOG_WARN("Detected file descriptor >= FD_SETSIZE or < 0. "
                  "Download may slow down or fail.");
      continue;
    }
```
fd 值超过 `FD_SETSIZE`（典型 1024）根本塞不进位图，只能跳过。顺带一提 Windows（`__MINGW32__`）分支里还有一个 `dummySocket_`：Winsock 的 `select` 不允许三个集合全空，所以专门开一个哑 socket 常驻集合（[L156-L159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc#L156-L159)、[L198-L204](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SelectEventPoll.cc#L198-L204) 的 `efds`）。

**四后端对照总表：**

| 维度 | EpollEventPoll | KqueueEventPoll | PortEventPoll | SelectEventPoll |
|---|---|---|---|---|
| 等待调用 | `epoll_wait` | `kevent`(取事件) | `port_getn` | `select` |
| 事件结构 | `epoll_event`（位图） | `kevent`（单过滤器） | `port_event_t`（poll 位） | `fd_set` 位图 |
| 注册 | `epoll_ctl` 持久 | `EV_SET`+`kevent` 持久 | `port_associate` **一次性** | 无（每次 poll 前重建） |
| 回指 entry | `data.ptr` | `udata` | `portev_user` | 无（遍历 map 自查） |
| IEV_* 来源 | `EPOLLIN` 等 | `POLLIN` 等 | `POLLIN` 等 | 不用（直接 `EVENT_*`） |
| 错误/挂断 | 报 `EPOLLERR/HUP` | 不映射 | 报 poll 位 | POSIX 不报（MINGW 用 `efds`） |
| 典型坑 | `MOD` 需回退 `ADD` | `udata` 类型兼容 | `port_getn` 信号怪癖 | `FD_SETSIZE` 上限 |

#### 4.4.3 代码实践

1. **实践目标**：感受「select 是兜底、epoll 是首选」。
2. **操作步骤**：在 Linux 上分别运行 `./src/aria2c --event-poll=epoll -d /tmp <URL>` 与 `./src/aria2c --event-poll=select -d /tmp <URL>`（同一 URL、加 `--allow-overwrite=true`），用 `strace -f -e trace=epoll_wait,select ./src/aria2c ...` 各跑一次，并用 `strace -c` 统计系统调用直方图。
3. **观察现象**：epoll 版几乎只有 `epoll_wait`；select 版每次等待前还有一组 `select` 调用且用户态要做 `FD_ISSET` 扫描。
4. **预期结果**：小文件下载两者差异不明显；若构造上百并发连接（如 BT），select 版的 CPU 占用应更高。待本地验证。

#### 4.4.4 小练习与答案

**练习 1**：如果把 `EpollEventPoll` 的 `poll` 里 `reinterpret_cast<KSocketEntry*>(epEvents_[i].data.ptr)` 这行删了，改成像 select 那样拿 fd 查 `socketEntries_` map，功能还正确吗？
**答案**：功能仍可正确（fd → entry 查表），但每次就绪事件多一次 O(log n) 查找，且 `epoll_event.data` 是联合体，改放 fd 就放不了指针。aria2 选择直接塞指针，属于典型的「空间换查找」。

**练习 2**：为什么只有 `PortEventPoll` 在**分发循环内**重新注册，而 epoll/kqueue 不用？
**答案**：`port_associate` 是 one-shot 语义——事件上报后关联自动解除；epoll（level-triggered 模式）与 kqueue 的 `EV_ADD` 都是持久注册，只要兴趣不变就无需重挂。

**练习 3**：`SelectEventPoll` 为什么不定义 `IEV_*` 常量也能工作？
**答案**：`Event.h` 模板里的 `CommandEvent::processEvents` 通过 `EventPoll::IEV_READ` 取「就绪位」的编码。select 后端自己拼事件（`FD_ISSET` → `EVENT_READ`），就绪位编码就是抽象位本身；它因此在 `.cc` 里内联了一套直接使用 `EventPoll::EVENT_*` 的非模板 `CommandEvent`，绕开了对 `IEV_*` 的依赖。

### 4.5 后端选择与 commands_ 延迟执行机制

#### 4.5.1 概念说明

最后把两个问题接起来：① `DownloadEngine` 手里的 `EventPoll` 到底是哪个后端、由谁决定；② 事件回调「只置位不执行」之后，命令是在哪里、以什么条件被真正执行的——这就是规格里说的 **commands_ 延迟执行机制**。

#### 4.5.2 核心流程

```text
DownloadEngineFactory::newDownloadEngine
  → createEventPoll(op)                     # 读 --event-poll + 编译宏
  → DownloadEngine(eventPoll)               # 引擎持有唯一后端实例
DownloadEngine 暴露 4 个注册包装:
  addSocketForReadCheck(sock, cmd)    → eventPoll_->addEvents(fd, cmd, EVENT_READ)
  addSocketForWriteCheck(sock, cmd)   → eventPoll_->addEvents(fd, cmd, EVENT_WRITE)
  (对应的 delete 版本同理)
主循环每圈:
  waitData() → eventPoll_->poll(tv)
      ↳ 事件回调: setStatusActive() + readEventReceived()/...
  executeCommand(commands_, STATUS_ACTIVE)   # 平时只跑被唤醒的
  （每 refreshInterval 一次全量 STATUS_ALL，见 u2-l1）
      ↳ com->transitStatus() → com->execute() → 成功则析构，失败则 clearIOEvents()
命令内部消费标志:
  AbstractCommand::shouldProcess() 看 readEventEnabled()/writeEventEnabled()
  决定这一轮要不要真的去 read/write socket
```

#### 4.5.3 源码精读

后端选择是「运行时选项 × 编译期宏」的二维决策：宏决定**哪些分支存在**，选项决定**走哪条**：

[DownloadEngineFactory.cc:L87-L145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L87-L145)
```cpp
std::unique_ptr<EventPoll> createEventPoll(Option* op)
{
  const std::string& pollMethod = op->get(PREF_EVENT_POLL);
#ifdef HAVE_EPOLL
      if (pollMethod == V_EPOLL) {
    auto ep = make_unique<EpollEventPoll>();
    if (!ep->good()) {
      throw DL_ABORT_EX("Initializing EpollEventPoll failed."
                        " Try --event-poll=select");
    }
```
每个后端构造后都检查 `good()`（fd 是否创建成功），失败即抛异常并**提示用户退回 select**——因为 select 无条件可用。默认值同样按宏优先级在选项定义处敲定：

[OptionHandlerFactory.cc:L284-L298](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L284-L298)
（`PREF_EVENT_POLL` 的默认值依次取 `HAVE_EPOLL` → `V_EPOLL`、`HAVE_KQUEUE` → `V_KQUEUE`、`HAVE_PORT_ASSOCIATE` → `V_PORT`、`HAVE_LIBUV` → `V_LIBUV`、`HAVE_POLL` → `V_POLL`，都无则 `V_SELECT`。）

引擎侧的四个注册包装把「socket 对象 + 命令」翻译成「fd + 命令 + 事件位」：

[DownloadEngine.cc:L202-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L202-L221)
```cpp
bool DownloadEngine::addSocketForReadCheck(
    const std::shared_ptr<SocketCore>& socket, Command* command)
{
  return eventPoll_->addEvents(socket->getSockfd(), command,
                               EventPoll::EVENT_READ);
}
...
bool DownloadEngine::addSocketForWriteCheck(
    const std::shared_ptr<SocketCore>& socket, Command* command)
{
  return eventPoll_->addEvents(socket->getSockfd(), command,
                                EventPoll::EVENT_WRITE);
}
```

`waitData` 只是把主循环的刷新间隔换算成超时并转交：

[DownloadEngine.cc:L187-L200](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L187-L200)
（`noWait_` 为真时超时为 0，即「只收割已有事件不等新事」——上一讲提过的加速出口。）

延迟执行的消费端在 `executeCommand` 帮助函数：

[DownloadEngine.cc:L118-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L118-L139)
```cpp
void executeCommand(std::deque<std::unique_ptr<Command>>& commands,
                    Command::STATUS statusFilter)
{
  size_t max = commands.size();
  for (size_t i = 0; i < max; ++i) {
    auto com = std::move(commands.front());
    commands.pop_front();
    if (!com->statusMatch(statusFilter)) {
      com->clearIOEvents();
      commands.push_back(std::move(com));
      continue;
    }
    com->transitStatus();
    if (com->execute()) {
      com.reset();          // 返回 true → 命令销毁
    }
    else {
      com->clearIOEvents(); // 返回 false → 命令已自行 requeue，抹掉本轮 IO 标志
      com.release();
    }
  }
}
```
三处要点：① 未通过状态过滤的命令**先 `clearIOEvents()` 再塞回队尾**——IO 标志描述的是「最近一轮 poll 发生了什么」，跨轮残留会造成幻事件；② `transitStatus()` 把 ACTIVE 打回 INACTIVE（[Command.cc:L50-L58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.cc#L50-L58)，REALTIME 除外），所以一个命令若要持续被调度，要么下一轮再被事件唤醒，要么自己 `setStatusRealtime()`；③ `execute()` 返回 false 意味着命令把自己挂回队列并**保持已注册的事件监听**，等下次事件再醒——这正是 u2-l1 讲过的「返回 false 须已自我 requeue」约定在事件视角下的样子。

标志位的物理载体就是 Command 基类的四个 bool：

[Command.h:L59-L71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.h#L59-L71)
```cpp
  bool readEvent_;
  bool writeEvent_;
  bool errorEvent_;
  bool hupEvent_;
...
  bool readEventEnabled() const { return readEvent_; }
```
置位/清除的实现是四个一行函数：[Command.cc:L62-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.cc#L62-L76)。

命令真正干活前用这些标志决定「这轮要不要碰 socket」：

[AbstractCommand.cc:L141-L159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L141-L159)
```cpp
bool AbstractCommand::shouldProcess() const
{
  if (checkSocketIsReadable_) {
    if (readEventEnabled()) {
      return true;
    }
    ...
  }

  if (checkSocketIsWritable_ && writeEventEnabled()) {
    return true;
  }
```

**`read_event_` 与 `write_event_` 的用途差异**（综合实践要用）：

| 标志 | 语义 | 典型消费者 |
|---|---|---|
| `read_event_` | 内核收缓冲区有数据（HTTP 响应、BT 消息、FTP 控制行） | `HttpDownloadCommand`、`PeerAbstractCommand` 等：醒来后 `recv` 读数据 |
| `write_event_` | 内核发送缓冲区可写。两种场景：① **非阻塞 `connect()` 完成**的信号（连接成功或失败都会变可写）；② 之前满的发送缓冲腾出空间可以继续发 | `HttpInitiateConnectionCommand`（等连接建立）、`BackupIPv4ConnectCommand`（[BackupIPv4ConnectCommand.cc:L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L90) 判断 `writeEventEnabled()` 得知备用连接成功） |

即：**读事件驱动「数据搬运」命令，写事件驱动「连接建立/发送」命令**。

#### 4.5.4 代码实践

1. **实践目标**：观察「命令醒来时带了什么 IO 标志」。
2. **操作步骤**：`AbstractCommand::execute` 开头本来就有一条调试日志（[AbstractCommand.cc:L181-L184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L181-L184)，打印 `socket: read:%d, write:%d, hup:%d, err:%d`）。直接运行 `./src/aria2c --log=io.log --log-level=debug -d /tmp <HTTP URL>`，然后 `grep -c "socket: read:1" io.log` 与 `grep -c "socket: read:0, write:1" io.log`。
3. **观察现象**：下载初期能找到 `write:1` 的行（连接建立阶段），数据传输期几乎全是 `read:1`。
4. **预期结果**：与上表一致——先写事件（connect 完成）后读事件（响应数据）接力。无需改代码即可完成。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么事件回调里不直接 `command->execute()`，而要绕一圈「置位 → 队列 → 过滤 → execute」？
**答案**：① poll 阶段正处于 `socketEntries_` 等后端数据结构的遍历/使用中，回调若触发命令销毁并注销事件，会一边遍历一边修改容器；② 单次 poll 返回的事件可能属于同一命令的不同 fd，先合并标志再统一执行能批量处理；③ 状态过滤（STATUS_ACTIVE/ALL）与实时命令优先级等调度策略需要一个集中的执行点。延迟执行把「事件检测」与「业务执行」解耦。

**练习 2**：`executeCommand` 中命令返回 false 时调用 `com.release()`（放弃 `unique_ptr` 所有权），为什么不是内存泄漏？
**答案**：u2-l1 讲过的约定——返回 false 的命令必须已把自己 `addCommand` 挂回队列（由另一个 `unique_ptr` 接管所有权），此处 `release()` 只是放弃本容器的那份引用，对象仍被队列持有。

## 5. 综合实践

把本讲所有内容串成一张图——**一次 socket 可读事件从 `epoll_wait` 返回到对应 Command 被调度的完整时序**。

**任务**：精读 [EpollEventPoll::poll](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L106-L143)，对照 4.2/4.5 的源码，画出时序图并回答 `read_event_` 与 `write_event_` 的用途差异。

参考时序图（文字版，读者应能对每一行指出对应源码行号）：

```text
DownloadEngine          EpollEventPoll             内核epoll         KSocketEntry/CommandEvent      Command
     |                        |                        |                        |                       |
     |--run() 每圈------------>|                        |                        |                       |
     |  waitData()            |                        |                        |                       |
     |  eventPoll_->poll(tv)--|                        |                        |                       |
     |                        |--epoll_wait(epfd,...)->|                        |                       |
     |                        |    (EINTR 则重试)      |  [HTTP 响应数据到达 socket]                       |
     |                        |<--res + 就绪 epoll_event 列表------------------  |                       |
     |                        |  p = (KSocketEntry*)ev.data.ptr (注册时塞的指针) |                       |
     |                        |--processEvents(EPOLLIN)------------------------->|                       |
     |                        |                        |      对每条 CommandEvent 订阅:                 |
     |                        |                        |      IEV_READ & EPOLLIN → 真                   |
     |                        |                        |------setStatusActive()------------------------->| status_=ACTIVE
     |                        |                        |------readEventReceived()---------------------->| readEvent_=true
     |                        |<--(poll 返回, 未执行任何命令)                                            |
     |<--(waitData 返回)-------|                        |                        |                       |
     |  executeCommand(commands_, STATUS_ACTIVE)       |                        |                       |
     |  遍历队列: statusMatch(ACTIVE)? ─ 否 → clearIOEvents + 回队尾                                            |
     |                    是 → transitStatus() (ACTIVE→INACTIVE)                                              |
     |                         → com->execute()                                                              |
     |                            AbstractCommand::execute:                                                  |
     |                            日志 "socket: read:1 ..." (L181)                                           |
     |                            shouldProcess(): readEventEnabled()==true → 读 socket、写盘                |
     |                         execute()==true → 命令析构; ==false → clearIOEvents + 命令已自行 requeue      |
```

**交付物**：

1. 手绘或用工具整理上图，并为每个箭头标注源码位置（提示：关键行号分布在 [EpollEventPoll.cc:L112-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L112-L121)、[Event.h:L93-L111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L93-L111)、[DownloadEngine.cc:L118-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L118-L139)）。
2. 用三五行文字回答：`read_event_` 服务数据搬运（`recv` 循环），`write_event_` 服务非阻塞连接完成与发送缓冲可写（参考 4.5.3 的对照表，并援引 [BackupIPv4ConnectCommand.cc:L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L90) 作为「写事件 = 连接完成信号」的实例）。
3. 可选验证：配合 4.5.4 的 `--log-level=debug` 日志，找出一条 `write:1` 的命令行（连接阶段）与一条 `read:1` 的命令行（下载阶段），在图上标出它们各自的触发路径。待本地验证。

## 6. 本讲小结

- `EventPoll` 抽象只有 4 个事件位（`EVENT_READ/WRITE/ERROR/HUP`，注意源码中没有 `A2_EVENT_NET_*` 这个前缀）和 `poll`/`addEvents`/`deleteEvents`（+异步 DNS 两方法）这五个契约，上层完全不感知平台。
- 共性回调骨架在 `Event.h`：`SocketEntry` 是「fd → (Command, 事件位) 订阅表」，同一命令的多次注册按位合并；`CommandEvent::processEvents` 是所有后端共用的分发逻辑。
- 抽象位 → 平台原语的映射是双向的：注册方向靠每个后端的 `translateEvents`，就绪方向靠每个后端类的 `IEV_*` 常量表（epoll 用 `EPOLLIN`、kqueue/port 借 `POLLIN`、select 干脆直接用抽象位）。
- 四后端各有一处「神差异」：epoll 需维护用户态镜像并处理 `MOD` 回退 `ADD`；kqueue 是过滤器模型（每 fd 两条 kevent、按 `filter` 反推事件）；port 是一次性注册（分发后必须重新 `port_associate`，还要绕 `port_getn` 的信号怪癖）；select 每轮全量重建 `fd_set` 且受 `FD_SETSIZE` 限制。
- 后端由 `--event-poll` 选项与编译宏共同决定，默认优先级 epoll > kqueue > port > libuv > poll > select，构造失败会提示退回 select。
- 事件回调「只置位不执行」：`setStatusActive()` + 四个 IO 标志，真正执行延后到 `executeCommand` 的状态过滤之后；`clearIOEvents` 在两个出口被调用以防跨轮幻事件；`read_event_` 驱动数据搬运命令，`write_event_` 驱动连接建立/发送命令。

## 7. 下一步学习建议

- **下一讲 u2-l3（Command 体系）**：本讲的落点是「命令被唤醒」，下一讲深入被唤醒的命令本身——`AbstractCommand` 状态机如何用 `readEventEnabled()` 决定走读分支还是写分支、命令间如何接力。
- **u2-l4（SocketCore 与异步 DNS）**：`EventPoll` 监视的 fd 从哪来？`SocketCore` 的非阻塞 `connect()` 如何与 `write_event_` 配合，c-ares 的 socket 如何经 `ADNSEvent` 进入本讲的事件循环。
- **源码延伸阅读**：`PollEventPoll` 与 `LibuvEventPoll` 是检验你是否真正理解骨架的好材料——带着「它的 `IEV_*` 是什么、注册是否持久、怎么回指 entry」三个问题去读，应当比读 epoll 更快。
