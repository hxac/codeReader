# u10-l2 SCPI 集成与 TCP 远程控制

## 1. 本讲目标

上一讲（u10-l1）我们读懂了 SCPI 框架本身：`SCPINode`/`SCPICommand` 如何构成命令树、解析器如何逐层吃名、回调如何返回结果。但那套框架只是一台「发动机」——本讲要回答的是：**这台发动机被装进了哪辆车、谁往里灌指令、结果从哪里吐出来**。

学完本讲，你应当能够：

1. 说出 `AppWindow::SetupSCPI()` 在根节点上注册了哪些命令（`*IDN`、`*RST` 与整个 `DEVice` 子树），以及设备连接时还有哪些「动态挂载」的驱动专属命令。
2. 解释为什么 VNA、频谱仪、信号源三种模式天然形成三个顶级 SCPI 命名空间（`:VNA:`、`:SA:`、`:GENerator:`），并列举每个命名空间下的主要子节点。
3. 逐行读懂 `tcpserver.cpp` 这个不到 45 行的 TCP 服务器：它如何用「按行取数据」解决 TCP 粘包/半包问题、如何用「单连接 + 顶替」策略管理客户端、如何用两根信号线与 SCPI 框架解耦。
4. 用 `nc`/`telnet` 连上 SCPI 端口，在完全不开窗口（`--no-gui`）的情况下完成「查询识别 → 设置中心频率 → 读回」的远程配置闭环，并能对照源码解释每一次收发。

## 2. 前置知识

本讲默认你已读过 u10-l1（SCPI 命令框架），这里只补三个新概念：

- **命令面（command surface）**：一台仪器对外暴露的全部 SCPI 命令的集合。LibreVNA 的命令面不是一个类写死的，而是由 AppWindow、三个 Mode、TraceWidget、Calibration、Deembedding、设备驱动等许多对象在构造时「各自认领一块地」拼出来的。本讲要画的就是这张地图。
- **TCP 是字节流，不是报文流**：UDP 一次 send 对应一次 recv，而 TCP 只保证字节先后顺序不变，不保留你发送时的「边界」。客户端分三次发送的三条命令，可能在服务端一次 `read()` 里一起到达（**粘包**）；一条命令也可能被拆成两半到达（**半包**）。任何基于 TCP 的文本协议都必须自己定义「一条消息到哪里结束」——LibreVNA 的选择是**换行符 `\n` 分帧**。
- **粘包/半包的处理原语**：Qt 的 `QTcpSocket` 内部维护接收缓冲区，`canReadLine()` 在缓冲区里已经出现至少一个 `\n` 时返回真，`readLine()` 则取出「直到 `\n` 为止」的一段。两者配合，天然实现了按行切分：凑齐一行处理一行，不齐就留着等下次数据到达——这正是半包的克星。

另外回顾两个已学的结论（u2-l1、u10-l1）：

- `--no-gui` 启动时窗口不显示，但 Qt 事件循环照常运转，所有基于信号槽的服务（包括本讲的 TCP 服务器）照常工作；
- SCPI 服务器默认启用，监听端口 **19542**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `Software/PC_Application/LibreVNA-GUI/appwindow.cpp` | GUI 主窗口 | `SetupSCPI()` 根级命令面；`StartTCPServer()`/`StopTCPServer()` 接线；`--port`/`--no-gui` 参数 |
| `Software/PC_Application/LibreVNA-GUI/appwindow.h` | 主窗口声明 | 成员 `SCPI scpi` 与 `TCPServer *server` |
| `Software/PC_Application/LibreVNA-GUI/mode.cpp` / `mode.h` | 模式基类 | Mode 三重继承中的 `SCPINode`，构造时把自己挂到根 |
| `Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp` | VNA 模式 | `VNA::SetupSCPI()` 与 `:VNA:` 子树 |
| `Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp` | 频谱仪模式 | `SpectrumAnalyzer::SetupSCPI()` 与 `:SA:` 子树 |
| `Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp` | 信号源模式 | `Generator::setupSCPI()` 与 `:GENerator:` 子树 |
| `Software/PC_Application/LibreVNA-GUI/tcpserver.cpp` / `tcpserver.h` | TCP 服务器 | 全文精读：按行分帧、单连接策略、收发 |
| `Software/PC_Application/LibreVNA-GUI/scpi.cpp` | SCPI 框架 | 复习 `input()`/`process()` 中结果到 `output` 信号的路由 |
| `Software/PC_Application/LibreVNA-GUI/preferences.h` | 偏好设置 | SCPI 服务器默认开关与端口 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**AppWindow SCPI 装配**、**各模式 SCPI 节点**、**TCP 服务器与无头运行**。

### 4.1 AppWindow SCPI 装配：根级命令面

#### 4.1.1 概念说明

`AppWindow` 持有整棵 SCPI 命令树的根：成员变量 `SCPI scpi;`（[appwindow.h:L165](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.h#L165)）。回顾 u10-l1：`SCPI` 类本身就是根节点（构造时 `SCPINode("")`，空名字），它的构造函数注册了 IEEE 488.2 公共命令 `*CLS`/`*ESE`/`*ESR`/`*OPC`/`*WAI` 以及自省命令 `*LST`（[scpi.cpp:L18-L79](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L18-L79)）。

在公共命令之上，`AppWindow::SetupSCPI()` 负责注册**应用级**的命令面：仪器的身份（`*IDN`）、复位（`*RST`），以及一个庞大的 `DEVice` 子树——设备连接管理、偏好设置、参考时钟、模式切换、状态查询、能力上限查询。这些命令的回调全部是捕获 `this` 的 lambda，直接调用 AppWindow 的成员函数，所以「远程发一条命令」和「用户在窗口里点一下」最终走到同一份代码。

时序上有一个值得注意的细节：`SetupSCPI()` 在构造函数的第 190 行被调用（[appwindow.cpp:L190](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L190)），位于 `SetInitialState()`（第 192 行，创建三种模式）**之前**。这没问题，因为 `DEVice:MODE` 命令的回调在**被调用时**才去查 `modeHandler`，注册时不查。

#### 4.1.2 核心流程

AppWindow 根级命令面的组装顺序：

```text
AppWindow 构造函数
 ├─ SCPI scpi; 构造            → 根节点 + *CLS/*ESE/*ESR/*OPC/*WAI/*LST
 ├─ StartTCPServer(...)        → （L111-L122，见 4.3）
 ├─ SetupSCPI()  （L190）
 │   ├─ *IDN  / *RST
 │   └─ DEVice 子树
 │       ├─ DISConnect / CONNect / LIST / MODE / PREFerences / APPLYPREFerences
 │       ├─ SETUP{SAVE,LOAD}
 │       ├─ REFerence{OUT,IN}
 │       ├─ STAtus{UNLOcked,ADCOVERload,UNLEVel}
 │       └─ INFo{FWREVision,HWREVision,LIMits{...10 条}}
 ├─ SetInitialState()          → 三个 Mode 构造，各自把子树挂到根（见 4.2）
 └─ ConnectToDevice(...)（若有设备）
     └─ 驱动专属 SCPI 节点/命令动态挂到根（见 4.1.3 末尾）
```

一条 `*IDN?` 从网络到达后的完整旅程（承接 u10-l1）：

```text
TCP 字节 → TCPServer::received(line) → SCPI::input(line)
        → 入队 → SCPI::process() → 根节点 parse()
        → 命中 *IDN 的查询回调 → 返回字符串
        → 非空非错误 → emit SCPI::output(response)
        → TCPServer::send() → 加 '\n' 写回 socket
```

#### 4.1.3 源码精读

**身份查询 `*IDN`**——注册时只给查询回调（第一个参数 `nullptr` 表示「设置型回调不存在」，即 `*IDN xxx` 是非法命令）：

[appwindow.cpp:L523-L532](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L523-L532)

```cpp
scpi.add(new SCPICommand("*IDN", nullptr, [=](QStringList){
    QString ret = "LibreVNA,LibreVNA-GUI,";
    if(device) {
        ret += device->getSerial();
    } else {
        ret += "Not connected";
    }
    ret += ","+appVersion;
    return ret;
}));
```

这段代码拼出 IEEE 488.2 规定的四段式应答 `厂家,型号,序列号,版本号`。注意「序列号」一栏在未连接设备时是字符串 `Not connected`——这是远程脚本判断「GUI 活着但没接设备」的廉价手段。`*RST` 则调用 `SetResetState()` 与 `ResetReference()` 把整机复位（[appwindow.cpp:L533-L537](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L533-L537)）。

**`DEVice:CONNect`——远程上下线设备**：

[appwindow.cpp:L545-L565](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L545-L565)

```cpp
scpi_dev->add(new SCPICommand("CONNect", [=](QStringList params) -> QString {
    QString serial;
    if(params.size() > 0) {
        serial = params[0];              // 指定序列号
    } else if(UpdateDeviceList() > 0) {
        serial = deviceList[0].serial;   // 不带参数：连第一台
    } else {
        return SCPI::getResultName(SCPI::Result::Error);
    }
    if(!ConnectToDevice(serial)) { ... }
```

设置与查询双形态：`:DEVice:CONNect` 连接（可带序列号参数），`:DEVice:CONNect?` 返回当前设备序列号或 `Not connected`。配套的 `:DEVice:LIST?`（L566-L575）返回逗号分隔的可用设备序列号清单，`DISConnect`（L540-L544）断开。这意味着一个无头部署的 LibreVNA-GUI 可以被脚本完全托管：发现设备 → 选择连接 → 切模式 → 配扫描。

**`DEVice:MODE`——模式切换的远程入口**：

[appwindow.cpp:L687-L719](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L687-L719)

```cpp
if (params[0] == "VNA") {
    mode = modeHandler->findFirstOfType(Mode::Type::VNA);
} else if(params[0] == "GEN") {
    mode = modeHandler->findFirstOfType(Mode::Type::SG);
} else if(params[0] == "SA") {
    mode = modeHandler->findFirstOfType(Mode::Type::SA);
} else {
    return "INVALID MDOE";   // 原文如此——源码里的拼写错误
}
...
modeHandler->setCurrentIndex(index);
```

两个细节值得圈出来：其一，`DEVice:MODE` 的参数取值是 `VNA`/`GEN`/`SA`，而**模式子树的节点名**却是 `VNA`/`GENerator`/`SA`（见 4.2），两套拼写并不一致，写脚本时容易踩坑；其二，参数非法时返回的字符串 `INVALID MDOE` 是源码里一个未修正的拼写错误（MODE 误作 MDOE），由于它不等于任何标准结果名，解析器会把它当普通查询应答发回客户端、**不会**置错误标志——这是读源码才能发现的「彩蛋」。

**`DEVice:STAtus` 与 `DEVice:INFo`——状态与能力上限**：

[appwindow.cpp:L720-L742](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L720-L742) 把设备驱动的三个 Flag（失锁 `UNLOcked`、ADC 过载 `ADCOVERload`、激励电平未达标 `UNLEVel`）逐个包装成返回 `true`/`false` 的查询，回调里统一以 `device` 是否为空做守卫。[appwindow.cpp:L743-L790](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L743-L790) 则把 `DeviceDriver::Info::Limits`（u3-l1 学过的能力协商结构）逐项暴露为 `INFo:LIMits:MINFrequency?`、`MAXPoints?`、`MINIFBW?` 等 10 条查询——远程脚本在配置扫描前应当先查这些上限，避免把设备配置到能力之外。

**动态命令面：设备连接时挂载的驱动专属节点**。`DEVice` 子树之外，根节点上还有一批「时有时无」的命令。设备连接成功时：

[appwindow.cpp:L416-L421](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L416-L421)

```cpp
// Add SCPI nodes/commands
for(auto n : device->driverSpecificSCPINodes()) {
    scpi.add(n);
}
for(auto c : device->driverSpecificSCPICommands()) {
    scpi.add(c);
}
```

断开时对称地移除（[appwindow.cpp:L452-L467](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L452-L467)，连同 `temporaryDeviceNodes`/`temporaryDeviceCommands` 一并清空）。所以严格地说，LibreVNA 的 SCPI 命令面是**随连接状态呼吸的**：同一台 GUI，接 SSA3000X 和接 LibreVNA 时，`*LST?` 列出的命令清单并不相同。这正是 u3-l1「驱动通过 DeviceDriver 抽象向上贡献功能」在 SCPI 层的投影。

#### 4.1.4 代码实践：用 `*LST?` 给命令面拍快照

1. **实践目标**：不靠猜，拿到当前 GUI 实际存在的全部 SCPI 命令清单，并验证「命令面随连接状态变化」。
2. **操作步骤**：
   - 启动 GUI（无头即可：`./LibreVNA-GUI --no-gui`，或正常启动）；
   - 另开终端：`printf '*LST?\n' | nc 127.0.0.1 19542`；
   - 把输出存成文件 `connected.lst` 或 `disconnected.lst`；
   - 若有真实设备，用 `:DEVice:CONNect` 连上后再执行一次 `*LST?`，对比两份清单。
3. **需要观察的现象**：返回的是一长串以换行/分节形式列出的命令路径，其中应能找到 `:DEVice:LIMits:MAXPoints`、`:VNA:FREQuency:CENTer`、`:SA:TRACKing:LVL` 等（`*LST` 的输出格式见 u10-l1）。
4. **预期结果**：未连接设备时清单里没有驱动专属命令；连接后多出一批（LibreVNA 官方驱动会挂上手动控制等节点）。`*LST?` 的输出格式与命令数量随版本变化，**具体行数待本地验证**。
5. 本实践我没有实际运行，输出内容以你本机为准。

#### 4.1.5 小练习与答案

**练习 1**：`*IDN?` 的四段应答中，第三段在什么情况下不是设备序列号？
**答案**：未连接任何设备时，第三段是字符串 `Not connected`（[appwindow.cpp:L527-L529](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L527-L529)）。第四段则是 GUI 自身的版本号（编译时注入的 `appVersion`），与设备固件版本无关——设备固件版本要用 `:DEVice:INFo:FWREVision?` 查询。

**练习 2**：为什么 `SetupSCPI()` 在 `SetInitialState()` 之前调用却不会崩溃？
**答案**：因为 `DEVice:MODE` 等命令的 lambda 回调只在**命令到达被执行时**才解引用 `modeHandler`；注册阶段只是把 lambda 存进命令树。等到任何 SCPI 客户端连上来发命令时，三种模式早已在 `SetInitialState()` 中创建完毕。

**练习 3**：`DEVice:MODE` 的参数与三个模式子树的节点名分别是什么？为什么不一致也不会冲突？
**答案**：参数取值是 `VNA`/`GEN`/`SA`（[appwindow.cpp:L692-L700](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L692-L700)），子树节点名是 `VNA`/`GENerator`/`SA`。二者属于不同的解析路径：前者只是 `DEVice:MODE` 命令的一个字符串参数，由回调里的 if-else 链解释；后者才是命令树的节点名，由解析器匹配。不一致只是易用性瑕疵，不是功能缺陷。

### 4.2 各模式 SCPI 节点：三种模式 = 三个顶级命名空间

#### 4.2.1 概念说明

u2-l2 讲过 `Mode` 基类三重继承 `QObject`、`Savable`、`SCPINode`（[mode.h:L16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h#L16)）。当时关注的是生命周期与持久化；现在轮到第三重身份登场：**每个 Mode 对象本身就是命令树上的一个节点**，节点名在构造时传入：

[mode.cpp:L19-L28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L19-L28)

```cpp
Mode::Mode(AppWindow *window, QString name, QString SCPIname)
    : QObject(window),
      SCPINode(SCPIname),
      ...
{
    window->getSCPI()->add(this);
}
```

三个子类的构造调用分别是：

- `VNA`：`Mode(window, name, "VNA")`（[vna.cpp:L57-L58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L57-L58)）
- `SpectrumAnalyzer`：`Mode(window, name, "SA")`（[spectrumanalyzer.cpp:L45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L45)）
- `Generator`：`Mode(window, name, "GENerator")`（[generator.cpp:L8](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L8)）

于是命令树自然长出三个顶级命名空间：`:VNA:`、`:SA:`、`:GENerator:`。关键结论（承接 u10-l1）：**子树在构造时就挂到根，与模式是否处于激活状态无关**——即使当前是 VNA 模式，`:SA:FREQuency:SPAN 1000000` 依然可解析、依然会修改 SA 模式的设置结构。但要注意（u7-l1）：只有**激活且正在运行**的模式才会真正向设备下发配置（`VNA::ConfigureDevice` 开头有 `if(running)` 守卫，[vna.cpp:L1974-L1982](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1974-L1982)）。所以远程脚本的标准动作序列是：先 `:DEVice:MODE VNA` 切模式，再 `:VNA:ACQuisition:RUN` 启动，再配参数（或先配参数再 RUN）。

各模式的命令还有一个共同设计：**回调直达 `Set*` 槽**。u7-l1/u7-l2 学过，模式的设置以 `Settings` 结构为唯一事实来源，UI 控件和 SCPI 命令都是往这个结构里写值的「前端」。所以鼠标拖动和 SCPI 写入走的是同一条 100ms 防抖 → `ConfigureDevice` 的链路，不存在两套状态。

#### 4.2.2 核心流程

`:VNA:` 子树的组装（`VNA::SetupSCPI()` 在构造函数 [vna.cpp:L647](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L647) 被调用）：

```text
:VNA:
 ├─ SWEEP           （FREQUENCY/POWER 扫描类型）
 ├─ FREQuency{SPAN, START, CENTer, STOP, FULL, ZERO}
 ├─ POWer{START, STOP}        （功率扫描）
 ├─ SWEEPTYPE
 ├─ ACQuisition{RUN, STOP, IFBW, DWELLtime, POINTS, AVG, AVGLEVel, FINished, LIMit, SINGLE, FREQuency?, POWer?, TIME?}
 ├─ STIMulus{LVL, FREQuency}
 ├─ TRACe{...}                （traceWidget，整个 Trace 数据子树）
 ├─ CALibration{..., BUSy}    （cal 对象，校准子树）
 └─ DEEMBedding{...}          （deembedding 对象，去嵌入子树）
```

注意最后三行：`SCPINode::add(traceWidget)`、`add(&cal)`、`add(&deembedding)`（[vna.cpp:L1658-L1664](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1658-L1664)）——它们不是 `VNA::SetupSCPI()` 里 `new` 出来的普通节点，而是把已有的**对象本身**（分别是 `SCPINode("TRACe")`、`SCPINode("CALibration")`、`SCPINode("DEEMBedding")`）挂进来。这是「组合优先于重新注册」的做法：TraceWidget、Calibration、Deembedding 各自管理自己的子树（比如每个校准件在构造时就给自己挂了 SCPI 命令，见 calstandard.cpp 中大量的 `setupSCPI()`），VNA 只是提供挂载点。

`:SA:` 与 `:GENerator:` 子树同理，规模更小。

#### 4.2.3 源码精读

**`:VNA:FREQuency:CENTer`——设置/查询双形态的典型写法**：

[vna.cpp:L1478-L1488](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1478-L1488)

```cpp
scpi_freq->add(new SCPICommand("CENTer", [=](QStringList params) -> QString {
    unsigned long long newval;
    if(!SCPI::paramToULongLong(params, 0, newval)) {
        return SCPI::getResultName(SCPI::Result::Error);
    } else {
        SetCenterFreq(newval);
        return SCPI::getResultName(SCPI::Result::Empty);
    }
}, [=](QStringList) -> QString {
    return QString::number((settings.Freq.start + settings.Freq.stop)/2, 'f', 0);
}));
```

这是全仓库 SCPI 命令的「标准句式」，三段结构：参数解析（失败返回 `Error`）→ 调用既有 `Set*` 槽 → 返回 `Empty`；查询回调则从 `settings` 结构直接算答案。注意中心频率的查询值是 `(start+stop)/2` 现算出来的——设置 `SetCenterFreq` 保持 span 不动平移窗口，所以「写 1 GHz 再读」必然读到 1 GHz（前提是不超出设备 Limits 被夹取）。同组的 `SPAN`/`START`/`STOP`（[vna.cpp:L1456-L1499](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1456-L1499)）句式完全一致，`FULL`/`ZERO` 则只有设置形态（查询回调为 `nullptr`，[vna.cpp:L1500-L1509](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1500-L1509)）。

**`:VNA:ACQuisition`——启停与采集参数**：`RUN`/`STOP` 控制扫描启停，`IFBW`/`POINTS`/`AVG`/`DWELLtime` 设置中频带宽、点数、平均与驻留（[vna.cpp:L1549-L1632](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1549-L1632)）。其中 `FINished?`（L1608-L1610）查询单次扫描是否完成，是脚本「发启动 → 轮询完成 → 取数据」三步曲的同步原语。

**`:SA:` 子树**（[spectrumanalyzer.cpp:L950-L1007](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L950-L1007) 起）：`FREQuency` 节点与 VNA 的同名同构；频谱仪特有的部分是 `TRACKing`（跟踪源）子树——`ENable`/`Port`/`LVL`/`OFFset` 四条命令（[spectrumanalyzer.cpp:L1121-L1177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L1121-L1177)）。容易看走眼的一处：`NORMalize`（归一化）节点是挂在 `TRACKing` **下面**的（`scpi_tg->add(scpi_norm)`，[spectrumanalyzer.cpp:L1178-L1179](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L1178-L1179)），所以完整路径是 `:SA:TRACKing:NORMalize:ENable` 而不是 `:SA:NORMalize:ENable`。构造函数末尾同样把 `traceWidget` 挂进来（[spectrumanalyzer.cpp:L1211](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L1211)），SA 的 Trace 读取（`:SA:TRACe:DATA ...`）由此而来。

**`:GENerator:` 子树——最简形态**（[generator.cpp:L88-L123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L88-L123)）：

```cpp
add(new SCPICommand("FREQuency", [=](QStringList params) -> QString {
    ...
    central->setFrequency(newval);
    return SCPI::getResultName(SCPI::Result::Empty);
}, [=](QStringList) -> QString {
    return QString::number(central->getDeviceStatus().freq);
}));
```

只有 `FREQuency`/`LVL`/`PORT` 三条命令，且都直接 `add` 在模式节点上（没有中间层子节点）。与 VNA 不同，命令不写 `settings` 结构而是转发给中央控件 `central`（`SignalgeneratorWidget`）——因为 u7-l3 讲过，Generator 模式类自身无状态，控件值即唯一事实来源。两条路径写法不同，但「SCPI 只是另一个前端」的结论一致。

#### 4.2.4 代码实践：源码阅读型——为三条假想脚本命令找到真实路径

1. **实践目标**：不看文档，仅凭本节源码推导出三条常用命令的完整路径，并用 `*LST?` 验证。
2. **操作步骤**：
   - 先在纸上写出你预测的路径：①把 VNA 中心频率设为 1 GHz；②启动 VNA 扫描；③设置信号源输出电平 -10 dBm；
   - 依次核对 [vna.cpp:L1454](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1454)（FREQuency 节点挂在 `:VNA:` 下）、[vna.cpp:L1551](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1551)（RUN）、[generator.cpp:L101](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L101)（LVL）；
   - 用 `printf '*LST?\n' | nc 127.0.0.1 19542 | grep -i -e CENTer -e RUN -e LVL` 验证。
3. **需要观察的现象**：`*LST?` 输出中能匹配到这三条命令（注意大小写：u10-l1 讲过短形式是「砍掉注册名尾部的小写字母」，如 `CENTer` 的短形式是 `CENT`）。
4. **预期结果**：①`:VNA:FREQuency:CENT 1000000000`（或长形式 `:VNA:FREQuency:CENTer`）；②`:VNA:ACQuisition:RUN`（短形式 `:VNA:ACQ:RUN`）；③`:GENerator:LVL -10`（短形式 `:GEN:LVL -10`）。
5. 命令在 `*LST?` 中的确切呈现格式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么说「三个模式子树永远同时在线」？举一个由此产生的脚本陷阱。
**答案**：因为子树挂在各 Mode 的构造函数里（经 `Mode` 基类构造，[mode.cpp:L27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L27)），而三个 Mode 在 `SetInitialState()` 时就全部创建（u2-l2），激活只是切换谁占用设备。陷阱：当前是 SA 模式时发 `:VNA:FREQuency:CENT ...` 不会报错，它改的是 VNA 模式的 `settings`，但对设备无效（SA 激活时设备由 SA 控制，且 VNA 的 `ConfigureDevice` 有 `if(running)` 守卫）——命令「成功」了却什么也没发生。正确做法是先 `:DEVice:MODE VNA`。

**练习 2**：`:SA:TRACKing:NORMalize:ENable` 这条命令的节点层级是怎样的？为什么归一化挂在跟踪源下面？
**答案**：`SA`（模式节点）→ `TRACKing`（[spectrumanalyzer.cpp:L1121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L1121)）→ `NORMalize`（L1178，被 `scpi_tg->add` 挂入）。归一化（u7-l2）以跟踪源输出为参考做除法，物理上属于「用跟踪源做直通测量再归一」这一族操作，作者因此把它放在 TRACKing 子树下；这是历史/语义上的选择，不是协议强制。

**练习 3**：`VNA` 模式把 `traceWidget`、`cal`、`deembedding` 三个现成对象挂进子树（[vna.cpp:L1658-L1664](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1658-L1664)），这种写法相比在 `SetupSCPI()` 里逐条 `add(new SCPICommand(...))` 有什么好处？
**答案**：子树与对象共存亡、职责内聚——TraceWidget/Calibration/Deembedding 各自在自己的构造函数里维护自己的命令（例如每个校准件构造时调用自己的 `setupSCPI()`），模式类不必知道下级有哪些命令；对象增删内部结构（如增删校准件）时模式代码零改动。这本质上是把 u8-l5 讲过的「注册表 + 工厂」思想用在了 SCPI 树上。

### 4.3 TCP 服务器与无头运行

#### 4.3.1 概念说明

SCPI 框架与网络之间的桥梁是一个仅 43 行的类 `TCPServer`。它做三件事：

1. **监听**：在指定端口接受 TCP 连接（同一时刻只保留一个客户端）；
2. **收**：把 socket 收到的字节流按行切开，每行作为一个 `received(QString)` 信号发出；
3. **发**：提供 `send(QString)` 槽，把一行应答加上 `\n` 写回 socket。

它的精妙之处在于与 SCPI 框架**只通过两根信号线相连**，彼此完全不知道对方的存在——SCPI 框架不知道命令来自网络、串口还是测试代码，TCPServer 不知道手里这行字符串会被解析成什么。这就是为什么 u10-l1 说「与传输层仅两根信号连线解耦」。

而「无头运行」是这条链路的部署形态：`--no-gui` 让窗口永不显示（[appwindow.cpp:L209-L216](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L209-L216)），但 Qt 事件循环照常运转，TCP 服务器照常服务。一台没有显示器、只插着 LibreVNA 的机器，跑 `LibreVNA-GUI --no-gui` 就是一台网络可控的矢量网络分析仪——这台「仪器」的面板是 SCPI 命令树，它的网口是 19542 端口。

#### 4.3.2 核心流程

**启动决策**（构造函数最前端，早于 `ui->setupUi`）：

```text
parser 有 --port/-p ？ ──是──> StartTCPServer(命令行端口)；manualTCPport() 锁定
        │否
偏好 SCPIServer.enabled（默认 true）？ ──是──> StartTCPServer(偏好端口，默认 19542)
        │否
不启动（本次运行无 SCPI 服务）
```

**运行期数据流**（单线程，全部在 Qt 事件循环中）：

```text
客户端                    TCPServer                     SCPI 框架
  │  "*IDN?\n"              │                              │
  │ ─────── TCP 字节 ─────> │ QTcpSocket 缓冲区             │
  │                         │ readyRead → while(canReadLine)
  │                         │   readLine → trimmed        │
  │                         │   emit received("*IDN?") ──> │ SCPI::input()
  │                         │                              │ 入队 → process()
  │                         │                              │ parse → 回调执行
  │                         │                              │ emit output("LibreVNA,...")
  │                         │ <── send()：补 '\n' 写回 ─── │
  │ <───── "LibreVNA,...\n" ─│                              │
```

**连接生命周期**：

```text
新客户端 connect → newConnection 信号
  → delete 旧 socket（若有）→ socket = nextPendingConnection()   // 单连接、后来者顶替
  → 连 readyRead / stateChanged
  → 对端断开 → stateChanged(UnconnectedState) → deleteLater → socket = nullptr
```

#### 4.3.3 源码精读

**启动决策与接线**：

[appwindow.cpp:L111-L122](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L111-L122)

```cpp
if(parser.isSet("port")) {
    bool OK;
    auto port = parser.value("port").toUInt(&OK);
    if(!OK) {
        port = Preferences::getInstance().SCPIServer.port;  // 解析失败回退偏好端口
    }
    StartTCPServer(port);
    p.manualTCPport();
} else if(p.SCPIServer.enabled) {
    StartTCPServer(p.SCPIServer.port);
}
```

命令行 `-p/--port` 优先于偏好设置（选项定义在 [appwindow.cpp:L90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L90)），`manualTCPport()` 会让偏好对话框里的端口/开关控件变灰（preferences.cpp 中据此 disable），避免运行期被偏好逻辑反向关掉。默认值来自声明式描述表：`SCPIServer.enabled = true`、`SCPIServer.port = 19542`（[preferences.h:L387-L388](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L387-L388)）。注意这段代码位于构造函数第 111–122 行，而 `ui->setupUi(this)` 在第 140 行——**服务器先于界面存在**，远程能力不依赖任何窗口部件。

`StartTCPServer` 本体就是两根信号线：

[appwindow.cpp:L793-L798](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L793-L798)

```cpp
void AppWindow::StartTCPServer(int port)
{
    server = new TCPServer(port);
    connect(server, &TCPServer::received, &scpi, &SCPI::input);
    connect(&scpi, &SCPI::output, server, &TCPServer::send);
}
```

上行 `received → SCPI::input`，下行 `SCPI::output → send`，就这两句。换掉 TCP 换成别的传输（比如理论上加一个串口适配器），SCPI 框架一行不用改。停止与重启见 `StopTCPServer`（[appwindow.cpp:L800-L804](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L800-L804)，析构函数 L221 也调用它）与 `preferencesChanged`（[appwindow.cpp:L806-L818](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L806-L818)：开关变化启停、端口变化先停后起）。

**TCPServer 构造函数——监听、单连接、按行分发**（全文仅 43 行）：

[tcpserver.cpp:L5-L32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/tcpserver.cpp#L5-L32)

```cpp
TCPServer::TCPServer(int port)
{
    this->port = port;
    qInfo() << "Listening on port" << port;
    socket = nullptr;
    server.listen(QHostAddress::Any, port);
    connect(&server, &QTcpServer::newConnection, [&](){
        // only one connection at a time
        delete socket;
        socket = server.nextPendingConnection();
        connect(socket, &QTcpSocket::readyRead, [=](){ ... });
        connect(socket, &QTcpSocket::stateChanged, [&](QAbstractSocket::SocketState state){ ... });
    });
}
```

要点逐一拆解：

- `QHostAddress::Any`：同时监听 IPv4 与 IPv6 的所有网卡，`localhost` 与局域网 IP 都能连；
- **单连接策略**：注释 `only one connection at a time` 说的不是「拒绝第二个」，而是 `delete socket` 直接销毁旧连接、让新客户端**顶替**旧客户端。好处是实现极简、无需会话管理；代价是两个脚本同时连会互相踢，且旧 socket 是立即 `delete` 而非 `deleteLater`（对该对象而言此刻没有排队中的事件要处理，实践中可行，但属于需谨慎使用的写法）；
- 每次新连接重新 `connect` 两个信号（lambda 捕获的 `socket` 是成员变量指针，随顶替自动指向新连接）。

**接收路径——粘包/半包的解法**：

[tcpserver.cpp:L15-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/tcpserver.cpp#L15-L23)

```cpp
connect(socket, &QTcpSocket::readyRead, [=](){
    while(socket->canReadLine()) {
        auto available = socket->bytesAvailable();
        char data[available+1];
        socket->readLine(data, sizeof(data));
        auto line = QString(data);
        emit received(line.trimmed());
    }
});
```

这 8 行就是本讲的粘包答案，逐行对照三种场景：

| 到达情况 | 行为 | 解决的问题 |
|---|---|---|
| 一次到达 `"*IDN?\n:VNA:FREQ:CENT 1e9\n"`（两条粘在一起） | `canReadLine` 为真两次，`while` 循环取出两行，各发一次 `received` | **粘包**：多帧合包到达被逐行拆开 |
| 先到 `":VNA:FREQ"`、后到 `" 1e9\n"`（一条被拆两半） | 第一次 `readyRead` 时缓冲区无 `\n`，`canReadLine` 为假，循环体不执行；数据滞留缓冲区，待下半段到达后凑齐整行才处理 | **半包**：不足一帧就等 |
| 一行超长（客户端写入了大量无换行字节） | 缓冲区不断累积直到 `\n` 出现；`data` 按当前可读字节数分配（`char data[available+1]` 是 GCC 的变长数组扩展，`available` 是**全部**可读字节而非本行长度，属于宽松上界的分配） | 内存换简单性 |

`line.trimmed()` 去掉 `\n`，也会顺带去掉 telnet 客户端惯用的 `\r\n` 中的 `\r`——所以 `nc`（LF 结尾）和 `telnet`（CRLF 结尾）都能用。注意**没有超时和最大行长保护**：恶意或异常客户端可以只发字节不发换行，让缓冲区无限增长，这是把该服务部署到不可信网络前要自己补的加固点。

**发送路径与连接回收**：

[tcpserver.cpp:L24-L30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/tcpserver.cpp#L24-L30) 监视 `stateChanged`，对端断开（`UnconnectedState`）时 `deleteLater` 回收 socket 并把指针置空。[tcpserver.cpp:L34-L42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/tcpserver.cpp#L34-L42) 是应答出口：

```cpp
bool TCPServer::send(QString line)
{
    if (socket) {
        socket->write(QByteArray::fromStdString(line.toStdString()+'\n'));
        return true;
    } else {
        return false;
    }
}
```

每条应答显式补一个 `\n`——与接收侧的按行分帧严格对称；没有客户端时静默返回 false（应答丢弃，SCPI 框架不感知）。

**最后一个关键回路复习：哪些命令会有应答？** 回看 u10-l1 的 `SCPI::process()`（[scpi.cpp:L191-L232](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L191-L232)）：

[scpi.cpp:L215-L227](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L215-L227)

```cpp
auto response = lastNode->parse(cmd, lastNode);
if(response == getResultName(Result::Error)) {
    setFlag(Flag::CME);          // 只置错误标志，不回话
} ...
} else if(response == getResultName(Result::Empty)) {
    // do nothing                // 设置命令成功：无应答
} else {
    emit output(response);       // 只有查询结果才走网络
}
```

这就是远程使用最重要的一条经验：**设置类命令成功时没有任何应答字节，错误也不会回话（只默默置入 `*ESR?` 可读的事件寄存器）**。写脚本时不能「发一条等一条」，要么只对查询读应答，要么发完设置命令后主动用 `*ESR?` 检查有没有出错。

#### 4.3.4 代码实践：无头启动 + nc 三步远程配置

1. **实践目标**：把本讲全链路跑通——无头启动 GUI，用 `nc` 完成「查询识别 → 设置中心频率 → 读回」，并亲手制造一次粘包观察拆帧。
2. **操作步骤**：
   - 终端 A：`./LibreVNA-GUI --no-gui`（无硬件亦可；日志里应出现 `Listening on port 19542`，对应 [tcpserver.cpp:L8](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/tcpserver.cpp#L8) 的 `qInfo` 输出）；
   - 终端 B（逐条交互）：
     ```bash
     nc 127.0.0.1 19542
     *IDN?
     :DEVice:MODE VNA
     :VNA:FREQuency:CENTer 1000000000
     :VNA:FREQuency:CENTer?
     ```
   - 再做粘包实验（一次性灌入三条命令）：
     ```bash
     printf '*IDN?\n:VNA:FREQuency:CENTer 2000000000\n:VNA:FREQuency:CENTer?\n' | nc 127.0.0.1 19542
     ```
   - 可选半包实验：`printf '*IDN?' | nc 127.0.0.1 19542`（无换行），观察是否无应答。
3. **需要观察的现象**：
   - 交互模式下：`*IDN?` 立即回一行四段式应答；三条设置命令**均无任何回显**（`MODE`、`CENTer` 设置成功返回 `Empty`，不发字节）；最后的 `CENTer?` 回一行数字；
   - 粘包实验：尽管三条命令一次写入，仍按行依次处理，收到**两行**应答（IDN 应答 + 中心频率值）；
   - 半包实验：无换行则无应答（`canReadLine` 永假）。
4. **预期结果**（由源码推得，本机具体数值待本地验证）：
   - `*IDN?` → `LibreVNA,LibreVNA-GUI,Not connected,<GUI版本号>`（无设备时第三段为 `Not connected`，见 4.1.3）；
   - `:VNA:FREQuency:CENTer?` → `1000000000`（或粘包实验中为 `2000000000`）。理由：`SetCenterFreq` 保持 span 平移窗口，查询回调现算 `(start+stop)/2`（[vna.cpp:L1486-L1488](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1486-L1488)）；1/2 GHz 都在无设备时默认 Limits（VNA.maxFreq 默认 100 GHz，[devicedriver.cpp:L133-L145](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L133-L145)）之内，不会被夹取；
   - 若想观察设置失败的样子：发 `:VNA:FREQuency:CENTer abc`（参数非数字）——同样**没有应答**，随后发 `*ESR?` 应读到非零值（CME 位被置，[scpi.cpp:L215-L218](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L215-L218)）。
5. 实践中的任何一步若与你观察到的现象不符，回到 4.3.3 的源码逐行核对——尤其是「设置命令无应答」这一条，最容易让初学者误以为连接坏了。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TCPServer` 里完全没有出现 QThread 或 moveToThread，却能同时服务网络与 GUI？
**答案**：`QTcpServer`/`QTcpSocket` 是异步 IO 类：数据到达、新连接等事件都化作 Qt 信号，插入主线程（GUI 线程）的事件循环排队执行（u2-l1 讲过事件循环）。SCPI 命令的处理量很小（每行一次字符串解析与回调），单线程足够；真正的重活（设备通信）另有独立的 libusb 事件线程（u3-l2）。所以「无头运行」时虽然窗口不存在，事件循环仍在跑，网络服务不受影响。

**练习 2**：客户端 A 正在通过 19542 端口控制仪器，客户端 B 也连了上来。会发生什么？这有什么隐患？
**答案**：B 的连接触发 `newConnection`，处理函数第一句 `delete socket`（[tcpserver.cpp:L13-L14](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/tcpserver.cpp#L13-L14)）立刻销毁 A 的 socket，A 的连接被强制断开，B 顶替成为唯一客户端。隐患：多个脚本并发使用同一 GUI 时会互踢；且 A 端只会看到连接重置，得不到任何解释。部署时应保证同一时刻只有一个控制端。

**练习 3**：如果把 `while(socket->canReadLine())` 改成 `if(socket->canReadLine())`，粘包实验的表现会变成什么样？
**答案**：每次 `readyRead` 信号只取一行。当三条命令粘成一个 TCP 段到达时，第一个 `readyRead` 取走第一行（`*IDN?`）并处理，剩余两行留在缓冲区——如果这次事件循环里不再有新数据到达触发 `readyRead`，后两行会被**无限期搁置**，直到客户端再发任意字节才被「顺带」处理。所以 `while` 不可少：一次信号里必须把缓冲区中所有完整行清空。这是 Qt 按行读取的标准写法。

## 5. 综合实践：一台无头网络分析仪的远程配置会话

把本讲三个模块串成一个完整任务：**不开任何窗口，仅用网络把 GUI 配置成「1 GHz 中心频率、201 点、1 kHz IF 带宽的 VNA」并确认配置生效**，同时把每个往返报文记录成表。

**步骤**：

1. 启动无头服务（终端 A）：
   ```bash
   ./LibreVNA-GUI --no-gui            # 或加 -p 20000 自选端口
   ```
   确认日志出现 `Listening on port 19542`。
2. 建立会话并逐步下发命令（终端 B，`nc 127.0.0.1 19542`），按下表记录（「应答」列的预期值由源码推得）：

| 序号 | 发送 | 预期应答 | 源码依据 |
|---|---|---|---|
| 1 | `*IDN?` | `LibreVNA,LibreVNA-GUI,Not connected,<版本>` | appwindow.cpp:L523-L532 |
| 2 | `*LST?` | 完整命令清单（自查后续命令是否存在） | scpi.cpp:L75-L79 |
| 3 | `:DEVice:MODE VNA` | （无应答） | appwindow.cpp:L687-L707 |
| 4 | `:VNA:ACQuisition:POINTS 201` | （无应答） | vna.cpp:L1583-L1592 |
| 5 | `:VNA:ACQuisition:IFBW 1000` | （无应答） | vna.cpp:L1561-L1570 |
| 6 | `:VNA:FREQuency:CENTer 1000000000` | （无应答） | vna.cpp:L1478-L1488 |
| 7 | `:VNA:ACQuisition:RUN` | （无应答） | vna.cpp:L1551-L1556 |
| 8 | `:VNA:ACQuisition:POINTS?` | `201` | vna.cpp:L1583-L1592 查询回调 |
| 9 | `:VNA:ACQuisition:IFBW?` | `1000` | 同上 |
| 10 | `:VNA:FREQuency:CENTer?` | `1000000000` | vna.cpp:L1486-L1488 |
| 11 | `:DEVice:MODE?` | `VNA` | appwindow.cpp:L708-L719 |

3. 关掉 nc 重连一次，重复第 8–10 条：应答不变——因为设置存在 GUI 侧的 `settings` 结构里，与哪个客户端连接无关；这也验证了 TCPServer 无会话状态的设计。
4. 用 4.3.4 的粘包管线把第 8–10 条合并成一次 `printf ... | nc`，确认仍能收到三行应答，然后对照 [tcpserver.cpp:L15-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/tcpserver.cpp#L15-L23) 写 5–8 行文字解释：为什么合包发送不会串答案（每行独立 `emit received` → 独立解析 → 独立应答，且应答各自补 `\n`）。
5. （有硬件时）在第 7 步后加 `:VNA:ACQuisition:SINGLE ON` 与 `:VNA:ACQuisition:FINished?` 轮询，再用 `:VNA:TRACe:DATA? ...`（命令清单以 `*LST?` 为准）取数据；无硬件则跳过，标注「待本地验证」。

**验收标准**：报文表填写完整；能口头回答「哪几条命令没有应答、为什么」；粘包解释能准确说出 `canReadLine`/`readLine` 的分工。

## 6. 本讲小结

- **命令面是拼装出来的**：`SCPI` 根节点先由构造函数注册 IEEE 488.2 公共命令，`AppWindow::SetupSCPI()` 补上 `*IDN`/`*RST` 与 `DEVice` 子树（连接管理、模式切换、状态与能力上限），设备连接时还会动态挂载驱动专属节点、断开时移除——同一 GUI 的命令清单随连接状态变化。
- **三种模式 = 三个顶级命名空间**：`Mode` 基类继承 `SCPINode` 并在构造时把自己挂到根，于是 `:VNA:`、`:SA:`、`:GENerator:` 永远同时在线；子树内部再收编 `TRACe`/`CALibration`/`DEEMBedding` 等现成对象。命令回调直达各模式的 `Set*` 槽，SCPI 与鼠标是同一前端，但只有激活且运行中的模式才真正驱动设备。
- **TCP 服务器只有 43 行、两根信号线**：`received → SCPI::input` 与 `SCPI::output → send` 完成收发解耦；`while(canReadLine()) + readLine()` 以换行分帧一举解决粘包与半包，应答侧对称地补 `\n`。
- **单连接、后来者顶替**：新客户端会 `delete` 旧 socket 取而代之，无会话管理也无行长上限保护，是部署到共享/不可信网络前需要自行加固的点。
- **设置命令没有应答**：`SCPI::process` 只把查询结果发往 `output`；成功返回 `Empty`（静默）、失败只置 `*ESR?` 中的标志位——远程脚本必须按这个约定设计读写节奏。
- **无头即网络仪器**：TCP 服务器在 `ui->setupUi` 之前、由 `--port` 或偏好（默认启用、端口 19542）启动；`--no-gui` 关掉的只是窗口，事件循环与全部远程能力照常工作。

## 7. 下一步学习建议

本讲之后，你已经掌握了「控制面」的完整闭环。下一讲 u10-l3 转向「数据面」：

- **streamingserver.cpp 的流式输出**：与 SCPI 的一问一答不同，流式服务器逐点主动推送 JSON 行（u7-l4 已铺垫数据分级），配合本讲的 nc 会话即可组成「SCPI 配置 + socket 取数」的完整自动化方案；
- **Integrationtests 的 Python 测试库**：`Software/Integrationtests/tests/libreVNA.py` 把本讲的裸 TCP 会话封装成 Python 类，`TestBase.py`/`TestVNASweep.py` 展示了如何组织成回归测试——这是把本讲知识工程化的最佳范本；
- 若想继续深挖控制面，可重读本讲 4.1.3 的动态命令面一节，再对照 `librevnadriver.cpp` 中 `driverSpecificSCPINodes()` 的实现，看官方驱动往根上挂了哪些节点；
- 动手方向：给 4.3.3 指出的「无行长上限」补一个最大行长保护（超出即断开客户端），或在 `TCPServer` 上增加只读会话日志——两处改动都只涉及这 43 行，是练手的好尺寸。
