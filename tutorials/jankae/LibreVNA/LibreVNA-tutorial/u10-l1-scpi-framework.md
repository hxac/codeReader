# u10-l1 SCPI 命令框架：语法树与解析

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 LibreVNA-GUI 中 SCPI 命令树的层级组织方式：`SCPI`（根）→ `SCPINode`（树枝）→ `SCPICommand`（树叶）。
2. 跟踪一行 SCPI 文本（如 `:VNA:FREQuency:START 1000000`）从 TCP 字节流进入、被递归解析、最终触发某个 C++ lambda 回调的完整路径。
3. 区分同一个 `SCPICommand` 上的两种回调：command（设置）与 query（查询），以及它们的错误返回约定。
4. 亲手注册一条自定义命令 `:DEMO:HELLO?` 并验证其出现在命令树中、能被正确查询。

## 2. 前置知识

### 2.1 什么是 SCPI

SCPI（Standard Commands for Programmable Instruments，可编程仪器标准命令）是测试测量行业的事实标准远程控制语言。你在示波器、频谱仪、电源上几乎都能见到它。核心想法非常简单：

- 用**文本行**对话，比如发送 `*IDN?`，仪器回复 `LibreVNA,LibreVNA-GUI,serial,version`。
- 命令组织成一棵**树**，用冒号分隔层级，形如 `:VNA:FREQuency:START`。
- 每个命令有两种形态：**command**（无 `?` 后缀，执行一个动作/设置一个值）和 **query**（带 `?` 后缀，返回一个值）。

### 2.2 长短助记符（mnemonic）

SCPI 规范规定每个命令名由「大写前缀 + 可选小写尾巴」构成，例如 `FREQuency`：

- **短形式**：只写大写部分，`FREQ`。
- **长形式**：写全名，`FREQUENCY`（或任意大小写混合，匹配本不区分大小写）。

短形式是强制的最小拼写，长形式是为了人类可读。本讲会看到 LibreVNA 用一个极简的 `alternateName()` 函数（“砍掉尾部所有小写字母”）实现了这套约定。

### 2.3 与前面讲义的衔接

- **u2-l1** 告诉我们：`AppWindow` 构造函数的装配序列里，`SetupSCPI()`（appwindow.cpp:190）与 TCP 服务器在 `setupUi` 之前就绪——因为远程控制不依赖界面，`--no-gui` 无头模式下也必须可用。
- **u4-l1** 给出了关键定位：**SCPI 止于 GUI**。设备端固件只说二进制协议；GUI 是文本协议（SCPI）与二进制协议之间的翻译网桥。所以这套 scpi.cpp/scpi.h 是纯 PC 侧代码，与固件无关。
- **u2-l2** 讲过 `Mode` 基类三重继承 `QObject`、`Savable`、`SCPINode`——本讲揭晓第三重身份的内部机制。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [Software/PC_Application/LibreVNA-GUI/scpi.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.h) | 三个类的声明：`SCPICommand`（树叶）、`SCPINode`（树枝）、`SCPI`（根 + 解析引擎 + IEEE 488.2 状态寄存器） |
| [Software/PC_Application/LibreVNA-GUI/scpi.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp) | 全部实现：注册、递归解析、参数类型转换、错误与同步机制 |
| [Software/PC_Application/LibreVNA-GUI/appwindow.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp) | 命令树的"总装配车间"：`SetupSCPI()` 注册全局命令，并把各模式、各驱动的子树挂到根上 |

其它在本讲被引用但不精读的文件：`mode.cpp`（模式如何挂树）、`VNA/vna.cpp`（`:VNA:FREQuency:START` 的注册现场）、`preferences.h`（SCPI 服务器默认端口）。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **SCPICommand 与 SCPINode**——树叶与树枝的数据结构，以及注册机制。
2. **命令树解析**——一行文本如何逐层下降找到回调。
3. **错误处理约定**——返回字符串即错误码，加上 IEEE 488.2 状态寄存器与 `*OPC`/`*WAI` 同步。

### 4.1 SCPICommand 与 SCPINode：树叶与树枝

#### 4.1.1 概念说明

把 SCPI 命令树想象成一个文件系统：

- `SCPINode` 是**目录**。它有名字、可以嵌套子目录（`subnodes`）和文件（`commands`）。根目录是一个特殊的 `SCPI` 对象，它继承自 `SCPINode`，额外背负解析队列和状态寄存器。
- `SCPICommand` 是**文件**（叶子）。它持有**两个独立的 `std::function` 回调**：
  - `fn_cmd`：处理无 `?` 的设置型调用；
  - `fn_query`：处理带 `?` 的查询型调用。

  任意一个都可以是 `nullptr`——表示该命令"只可设置不可查询"（如 `*RST`）或反之（如 `*IDN?`）。`queryable()` 和 `executable()` 就是判空检查。

一个 `SCPICommand` 的名字理论上可以带冒号（如 `"DEMO:HELLO"`），因为注册逻辑会自动拆分并创建中间节点——这让你不必为了一条深层命令手工搭一串 `SCPINode`。

`SCPINode` 还提供四个**参数命令工厂**（`addDoubleParameter` 等）：把"一个 C++ 成员变量"直接包装成一对可读可写的 SCPI 命令，这是整个 GUI 里注册数以百计参数命令的捷径（u9-l1 见过校准件用它注册 `Z0`、`C0` 等系数）。

#### 4.1.2 核心流程

注册一条命令的流程：

```text
scpi.add(new SCPICommand("DEMO:HELLO", fn_cmd, fn_query))
        │
        ▼
SCPINode::addInternal(cmd, depth=0)          # 按冒号拆名："DEMO" "HELLO"
        │
        ├─ depth 未到最后一层？
        │     ├─ findSubnode("DEMO") 找不到 → new SCPINode("DEMO") 并挂上
        │     └─ 递归 subNode->addInternal(cmd, depth+1)
        │
        └─ depth == 最后一层：
              ├─ nameCollision("HELLO")？→ 命中则拒绝并 qWarning
              └─ commands.push_back(cmd)
```

长短助记符匹配规则（本实现）：

\[
\text{short}(s) = \text{删除 } s \text{ 尾部所有小写字母}
\]

两条名字 \(s_1, s_2\) 匹配，当且仅当 \(\text{full}(s_1)=\text{full}(s_2)\) 或 \(\text{short}(s_1)=\text{short}(s_2)\) 等四个组合任一成立（均不区分大小写）：

- 注册名 `FREQuency` → 匹配 `FREQ` 和 `FREQUENCY`；
- 注册名 `START`（全大写，无小写尾巴）→ 短形式 = 长形式 = `START`，**不**匹配 `STA`。

#### 4.1.3 源码精读

**SCPICommand：两个回调 + 名字**（[scpi.h:L10-L30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.h#L10-L30)）——构造函数接收 `name`、`fn_cmd`、`fn_query` 和 `convertToUppercase`（默认 true：参数在分发前转大写；字符串参数命令会传 false 以保留大小写）。`leafName()` 用 `split(":").back()` 取路径最后一段，供解析时匹配。

**SCPINode：目录结构**（[scpi.h:L32-L78](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.h#L32-L78)）——注意它禁止拷贝/移动（L39-L42），因为树节点持有裸指针父子关系；析构函数（scpi.cpp:272-286）会先把自己从父节点摘除，再逐个 `delete` 自己拥有的命令和子节点——**所有权自上而下，删除一个节点等于删除整棵子树**。四个 `add*Parameter` 工厂声明在 L51-L54。

**注册逻辑：自动创建中间节点 + 防重名**（[scpi.cpp:L505-L526](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L505-L526)）——`addInternal(SCPICommand*, depth)` 先按 `:` 拆名字；没到最后一层就 `findSubnode`，不存在则 `new SCPINode(parts[depth])` 现场造出中间目录再递归。这就是 `new SCPICommand("DEMO:HELLO", ...)` 一个对象就能注册两层路径的原因。

**重名检测**（[scpi.cpp:L417-L430](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L417-L430)）——`nameCollision` 用 `SCPI::match` 检查新名字是否与任何兄弟子节点或命令冲突（含长短形式冲突）。冲突时注册失败并打印 `qWarning`，**不会崩溃**，但命令静默缺失——这是调试"我注册的命令怎么不见了"时的第一怀疑点。

**长短助记符匹配**（[scpi.cpp:L82-L100](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L82-L100)）——`match` 做四次不区分大小写的比较（全名×全名、短名交叉）；`alternateName` 从尾部逐字符 `chop` 掉小写字母得到短形式。逻辑仅 10 行，却完整覆盖了 SCPI 助记符约定。

**参数命令工厂**（[scpi.cpp:L288-L305](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L288-L305)）——`addDoubleParameter` 用捕获引用 `[&param, setCallback]` 的 lambda 直接读写调用方的成员变量：cmd 回调做 `paramToDouble` 转换，成功则触发 `setCallback()`（通常是"应用新设置"的槽）；query 回调就是 `QString::number(param)`。`gettable`/`settable` 参数决定哪个回调传 `nullptr`。对照一个真实使用点：[calstandard.cpp:L299-L305](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calstandard.cpp#L299-L305) 用它把开路标准件的 `Z0`/`DELAY`/`C0..C3` 六个系数一次性注册成可远程读写的命令。

**字符串参数的大小写豁免**（[scpi.cpp:L347-L365](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L347-L365)）——`addStringParameter` 在 L364 显式传 `convertToUppercase = false`：数值/布尔参数转大写无伤大雅，但 Trace 名、文件名这类字符串必须原样传递。

#### 4.1.4 代码实践：数一数根节点直接挂了什么

这是一个纯源码阅读实践（无需硬件、无需编译）：

1. **实践目标**：搞清楚"命令树的第一层"有哪些分支，为下一模块的解析追踪做准备。
2. **操作步骤**：
   - 打开 [appwindow.cpp:L521-L543](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L521-L543)，列出 `SetupSCPI()` 里 `scpi.add(...)` 的每一项，标注它是 `SCPICommand`（叶子）还是 `SCPINode`（子树）；
   - 再看 [mode.cpp:L19-L28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L19-L28)：`Mode` 构造函数把 `this` 挂到 `window->getSCPI()` 上——每个模式都是根下的一个子树；
   - 查 [vna.cpp:L57-L58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L57-L58) 确认 VNA 模式的 SCPI 节点名是 `"VNA"`（第三个构造参数）。
3. **需要观察的现象**：根的直接子节点应包括 `*IDN`/`*RST` 等 IEEE 命令、`DEVice` 节点、以及三个模式子树（`VNA`、SA 模式、Generator 模式的 SCPI 名）。
4. **预期结果**：一张手绘的第一层树图。注意一个细节——模式子树在**模式构造时**就挂上，而不是激活时，所以即使当前激活的是频谱仪模式，`:VNA:FREQuency:START` 依然可解析。
5. 待本地验证部分：各模式的确切 SCPI 名可照同样方法在 `SpectrumAnalyzer`、`Generator` 构造函数里确认。

#### 4.1.5 小练习与答案

**练习 1**：注册名为 `CENTer` 的命令，哪些输入能匹配？哪些不能？
**答案**：匹配 `CENT`、`CENTER`、`cent`、`CentEr`（匹配全程不区分大小写）；不匹配 `CEN`（比短形式还短）或 `CENTERX`。

**练习 2**：为什么 `SCPINode` 要删除拷贝构造和移动构造？
**答案**：树节点通过裸指针持有 `parent`、`subnodes`、`commands` 三组关系。若允许拷贝/移动，会出现两个对象指向同一批子节点、析构时双重释放的问题。禁止拷贝/移动强制每个节点在树中地址唯一。

**练习 3**：`addBoolParameter` 的 query 回调返回什么字符串？由哪个函数定义？
**答案**：返回 `"TRUE"` 或 `"FALSE"`（scpi.cpp:328-345），字面量由 `SCPI::getResultName(SCPI::Result::True/False)` 定义（scpi.cpp:160-179）。

### 4.2 命令树解析：从一行文本到回调执行

#### 4.2.1 概念说明

解析引擎解决的问题是：把 `":VNA:FREQuency:START 1000000"` 这样一行文本，变成"在 VNA 模式的 FREQuency 节点上调用 START 命令的 cmd 回调，参数是 `["1000000"]`"。

设计上有三个要点：

1. **队列化串行执行**。`SCPI::input()` 是 Qt 槽，由 TCP 服务器直接调用；命令先入 `cmdQueue`，由 `process()` 逐条消化，两把 `QSemaphore` 保护队列与处理器的并发。远程命令永远一条接一条执行，不会重入回调。
2. **递归下降**。`SCPINode::parse()` 每次只消费一段冒号前的名字：这段若是子节点名就下钻递归；没有冒号了就说明到达叶子，在本节点的 `commands` 里找命令。
3. **分号与"当前节点"记忆**。SCPI 允许一行多条命令（`;` 分隔）。子命令若以 `:` 或 `*` 开头，路径从根重新算；否则**从上一条命令所在的节点续接**（`lastNode`）——这就是 `:VNA:FREQuency:START 1e6;STOP 2e6` 里 `STOP` 不用重写全路径的原因。

参数切分发生在叶子层：按空格分词、支持引号包裹（`"` 或 `'`）与反斜杠转义、命令名本身被解析成第一个"参数"后 `pop_front` 丢弃——一个略显古怪但自洽的小技巧。

#### 4.2.2 核心流程

```text
TCP 字节流
   │  (TCPServer::received 信号, appwindow.cpp:796)
   ▼
SCPI::input(line)                    # scpi.cpp:181  入队
   ▼
SCPI::process()                      # scpi.cpp:191  逐条出队
   │  cmd.split(";")
   ▼  对每个子命令：
   ├─ 首字符是 ':' 或 '*'？→ lastNode 重置为根
   ├─ 首字符是 ':'？→ 去掉这个冒号
   ▼
SCPINode::parse(cmd, lastNode)       # scpi.cpp:551  从 lastNode 开始递归下降
   │
   ├─ 取第一个空格前的词 = 命令名；找冒号位置 splitPos
   ├─ splitPos > 0：在 subnodes 里 match 段名 → 递归 parse(剩余部分)
   │      找不到 → 返回 "ERROR"
   └─ 无冒号（叶子层）：
         ├─ 逐字符切参数（引号/转义/空格）
         ├─ pop_front 丢掉命令名
         ├─ 末尾有 '?'？→ isQuery = true, 去掉 '?'
         ├─ 在 commands 里 match 命令名 → 命中：
         │      lastNode = 当前节点
         │      参数按需转大写
         │      isQuery ? c->query(params) : c->execute(params)
         └─ 找不到 → 返回 "ERROR"
   ▼
process() 对返回字符串分类：
   "ERROR"/"CMD_ERROR"/"QUERY_ERROR" → 置 SESR 的 CME 位
   "EXEC_ERROR"                      → 置 SESR 的 EXE 位
   ""(Empty)                          → 不输出（设置型命令的正常返回）
   其它任意字符串                      → emit output(response) 发回客户端
```

`:VNA:FREQuency:START 1000000` 的逐层下降（注意：代码里注册名是全大写 `START`，命令树中并不存在拼写为 `STARt` 的命令；由于匹配不区分大小写，写 `STARt` 同样命中，详见 4.2.4）：

| 层 | parse 调用者 | 输入 | 匹配的段 | 命中对象 |
|---|---|---|---|---|
| 1 | 根 `SCPI` | `VNA:FREQuency:START 1000000` | `VNA` | VNA 模式节点（mode.cpp:27 挂载，vna.cpp:58 命名） |
| 2 | VNA 节点 | `FREQuency:START 1000000` | `FREQuency` | `scpi_freq` 节点（vna.cpp:1454 创建） |
| 3 | FREQuency 节点 | `START 1000000` | `START` | SCPICommand（vna.cpp:1467 注册）→ cmd 回调 `SetStartFreq(1000000)` |

#### 4.2.3 源码精读

**入队与串行消化**（[scpi.cpp:L181-L232](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L181-L232)）——`input()` 用 `semQueue` 保护 `cmdQueue.append`，若处理器空闲（`semProcessing.available()`）就立即 `process()`。`process()`（L191-L232）在 `WAIexecuting` 为真时**暂停消化**（`*WAI` 同步，见 4.3）；对每个 `;` 子命令先按首字符决定是否回根（L207-L213），再调 `lastNode->parse`，最后按返回字符串分流（L215-L227）——这段"字符串即协议"的分类逻辑是错误处理的核心，4.3 详述。

**递归下降解析器**（[scpi.cpp:L551-L625](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L551-L625)）——L556 取命令名（第一个空格前的部分）；L557-L568 有冒号则在本层 `subnodes` 里用 `SCPI::match(n->leafName(), subnode.toUpper())` 找段名并递归；L570-L621 叶子层：先逐字符构建 `params`（L574-L598，处理 `\` 转义、引号包裹、空格分词），L600 `pop_front` 把命令名丢掉，L601-L605 检查尾部 `?`，L606-L621 在 `commands` 里匹配并调用 `c->query(params)` 或 `c->execute(params)`。注意 L607 匹配前把输入 `cmdName.toUpper()`——所以**客户端的大小写永远无关紧要**，短形式只能来自注册名的小写尾巴（4.1.2）。

**`:VNA:FREQuency:START` 的注册现场**（[vna.cpp:L1454-L1477](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1454-L1477)）——`VNA::SetupSCPI()` 先 `new SCPINode("FREQuency")` 挂到自己（VNA 节点）下，再给它注册 `SPAN`/`START`/`CENTer`/`STOP`/`FULL` 五个命令。`START` 的 cmd 回调（L1467-L1474）用 `SCPI::paramToULongLong` 转参数、成功则 `SetStartFreq(newval)` 返回 Empty；query 回调（L1475-L1477）返回 `settings.Freq.start`。这正是一条命令"设置 + 查询"双回调的范本。

**根级装配**（[appwindow.cpp:L521-L543](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L521-L543)）——`AppWindow::SetupSCPI()` 注册 `*IDN`（查询返回 `LibreVNA,LibreVNA-GUI,序列号,版本号`，L523-L532）与 `*RST`（复位 GUI 状态，L533-L537），随后创建 `DEVice` 子树（L538-L539 起）。此外驱动也能贡献命令：连接设备时 `driverSpecificSCPINodes()/Commands()` 被挂上（appwindow.cpp:415-421），运行中驱动还可经 `addSCPICommand` 信号动态增删（appwindow.cpp:359-373）——命令树是活的。

**与传输层的解耦**（[appwindow.cpp:L793-L798](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L793-L798)）——`StartTCPServer()` 只做两根连线：`TCPServer::received → SCPI::input`、`SCPI::output → TCPServer::send`。scpi.cpp 对 TCP 一无所知；换成串口或 stdin 喂 `input()` 一样工作。服务器的启动由 `--port <n>` 命令行参数或 Preferences 决定（[appwindow.cpp:L111-L122](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L111-L122)），默认**启用**、端口 **19542**（[preferences.h:L387-L388](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L387-L388)）。

#### 4.2.4 代码实践：手工推演解析路径

这是一个"人肉解析器"实践，无需硬件：

1. **实践目标**：不运行程序，仅凭 scpi.cpp 的解析逻辑，预测若干输入的命运，建立对匹配规则的肌肉记忆。
2. **操作步骤**：
   - 画出 4.2.2 的三层下降表，对象分别是根 → VNA 节点 → `scpi_freq` 节点 → `START` 命令；
   - 对下面每个输入，写下你认为的解析结果（命中/返回什么/是否置错误标志）：

     | 输入 | 你的预测 |
     |---|---|
     | `:VNA:FREQ:START 1e6` | ？ |
     | `:vna:frequency:start 1e6` | ？ |
     | `:VNA:FREQuency:STA 1e6` | ？ |
     | `:VNA:FREQuency:STARt 1e6` | ？ |
     | `:VNA:FREQuency:START;STOP 2e6` | ？ |
     | `:VNA:FREQuency:START?` | ？ |

   - 然后逐条对照 scpi.cpp 的 `match`/`alternateName`/`parse` 核对。
3. **需要观察的现象与预期结果**（推导答案）：
   - 第 1 条**命中**：`FREQ` 是 `FREQuency` 的短形式（`alternateName("FREQuency")=="FREQ"`）；
   - 第 2 条**命中**：输入被 `toUpper()` 后匹配，大小写全然无关；
   - 第 3 条**失败返回 `ERROR`**：注册名 `START` 全大写、短形式即 `START`，`STA` 比短形式还短——这是本实现与"任意前缀都行"的朴素设想的最大差异；
   - 第 4 条**命中**：`STARt`.toUpper() == `START`（讲义规格中 `STARt` 这种 SCPI 风格拼写能工作的原因）；
   - 第 5 条**双双命中**：`STOP` 无前导 `:`/`*`，从 `lastNode`（FREQuency 节点）续接，等效 `:VNA:FREQuency:STOP 2e6`；
   - 第 6 条返回当前起始频率数字（query 回调执行，`emit output`）。
4. **待本地验证**：以上推导基于源码逐行阅读，建议在第 5 节综合实践搭好 TCP 通道后实测复核。

#### 4.2.5 小练习与答案

**练习 1**：`process()` 中为什么在 `lastNode->parse(...)` 之前检查 `cmd[0] == '*'` 也要重置到根？
**答案**：`*IDN?`、`*RST` 这类 IEEE 488.2 通用命令不带前导冒号，直接注册在根上（scpi.cpp:18-79）。若不重置，`*:VNA:FREQuency:START;*IDN?` 的第二段会在 FREQuency 节点里找 `*IDN` 而失败。`*` 与 `:` 一样是"回根"信号。

**练习 2**：`parse()` 的参数切分为什么要把命令名也先塞进 `params` 再 `pop_front`？
**答案**：切分循环（L574-L598）对整个字符串逐字符处理（这样才能正确应对引号内空格与转义），命令名天然成为切出的第一个词。与其在循环前另行剥离命令名，不如切完后 L600 统一丢弃，两种逻辑合并成一套。

**练习 3**：如果客户端一次发来 `:DEVice:DISConnect\n:VNA:FREQuency:START 1e6\n`（两个换行包），会并发执行吗？
**答案**：不会。TCP 侧到达的每行各自触发 `SCPI::input()`，但 `process()` 被 `semProcessing` 串行化；即便两次 `input()` 在不同线程到达，命令也是逐条消化，回调不会重入。

### 4.3 错误处理约定：返回字符串、状态寄存器与同步

#### 4.3.1 概念说明

这个框架没有异常、没有错误码枚举穿透调用链——**错误就是一个特殊字符串**。回调返回 `QString`，由 `getResultName()` 把 `SCPI::Result` 枚举翻译成 `"ERROR"`、`"CMD_ERROR"` 等字面量。`process()` 拿到返回值后按字符串内容分类：错误类置状态位、Empty 静默、其余原样发回客户端。

在这之上是一套**IEEE 488.2 标准事件状态机制**（很多商业仪器同款）：

- **SESR**（Standard Event Status Register）：8 个事件位。本实现用到 `CME`（命令错误，如语法错/命令不存在）、`EXE`（执行错误，如参数非法）、`OPC`（操作完成）等（scpi.h:117-126）。
- **`*ESR?`**：读取并清零 SESR——客户端用它轮询"刚才到底错没错"。
- **`*ESE`/`*ESE?`**：设置/查询事件使能屏蔽 ESE（本实现存储了 ESE 但不产生服务请求中断）。
- **`*CLS`**：清状态。

还有一组**同步原语**解决"命令完成了没有"：

- **`*OPC`/`*OPC?`**：挂起操作未完成时，登记"完成时置 OPC 位/回 1"；已完成则立即生效。
- **`*WAI`**：暂停命令队列消化，直到所有挂起操作完成（`WAIexecuting` 让 `process()` 的 while 循环空转退出）。
- 节点用 `setOperationPending(true/false)` 报告自己有无长耗时操作；任一节点 pending 时 `isOperationPending()`（递归全树）为真。

最后是一个**非标准但极其实用的自省命令 `*LST?`**：遍历整棵命令树，列出所有可查询/可执行的命令全文。它是了解"这台仪器到底会说什么话"的最快方式。

#### 4.3.2 核心流程

错误分流的判定顺序（process() 内，scpi.cpp:215-227）：

| 回调返回字符串 | 含义 | 后续动作 |
|---|---|---|
| `""`（Empty） | 设置命令正常完成 | 不输出 |
| `ERROR` | 通用错误（参数转换失败等） | 置 CME 位 |
| `CMD_ERROR` | command 不可执行（fn_cmd 为空或 cmd 回调返回 ERROR） | 置 CME 位 |
| `QUERY_ERROR` | query 不可执行（fn_query 为空或 query 回调返回 ERROR） | 置 CME 位 |
| `EXEC_ERROR` | 业务层执行失败 | 置 EXE 位 |
| 其它任意字符串 | 查询结果 | `emit output(response)` 发回客户端 |

`SCPICommand::execute/query` 的"错误升级"链：

```text
回调内部返回 "ERROR"（如 paramToULongLong 失败）
        │
        ▼
execute(): "ERROR" → "CMD_ERROR"     # 让调用方知道这是"作为 command 被调用时"出的错
query():   "ERROR" → "QUERY_ERROR"
fn_cmd == nullptr → 直接 "CMD_ERROR"  # 对只读命令发设置指令
fn_query == nullptr → 直接 "QUERY_ERROR" # 对只写命令发查询
```

`*OPC`/`*WAI` 状态机：

```text
*OPC? 到达 ──┬─ isOperationPending()？ 否 → 立即回 "1"
             └─ 是 → OPCQueryScheduled=true, OCAS=true（暂不回复）
节点完成操作 ──→ setOperationPending(false) ──→ 向上找根（dynamic_cast<SCPI*>）
             ──→ SCPI::someOperationCompleted()
                    ├─ 全树已无 pending 且 OCAS：补发 output("1") / 置 OPC 位
                    └─ WAIexecuting？→ 恢复 process() 继续消化队列
```

#### 4.3.3 源码精读

**Result 枚举与字面量**（[scpi.h:L94-L102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.h#L94-L102)、[scpi.cpp:L160-L179](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L160-L179)）——注意 `Error` 分支与 `default` 合并，未列出的值统一变成 `"ERROR"`；`True`/`False`/`Empty` 也走这个通道，所以**回调永远不该把普通数据返回成这几个保留字**。

**execute/query 的错误升级**（[scpi.cpp:L627-L651](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L627-L651)）——判空给 `CMD_ERROR`/`QUERY_ERROR`；回调返回 `ERROR` 则按调用形态升级。业务代码因此只需表达"成功 or 出错"，错误类别由框架补全。

**IEEE 488.2 命令族**（[scpi.cpp:L18-L79](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L18-L79)）——SCPI 构造函数往根上注册五条：`*CLS`（L18-23，清 SESR/OCAS/OPC 登记）、`*ESE`/`*ESE?`（L25-35，参数须是 0-255）、`*ESR?`（L37-41，**读取并清零**——读一次就没了，这是 488.2 语义）、`*OPC`/`*OPC?`（L43-65，按 pending 状态决定立即生效还是登记）、`*WAI`（L67-73）。再加非标准的 `*LST?`（L75-79）。

**挂起操作的完成上报**（[scpi.cpp:L382-L401](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L382-L401)）——`setOperationPending(false)` 时沿 `parent` 指针爬到树根，`dynamic_cast<SCPI*>` 后调 `someOperationCompleted()`（L234-L255）：若全树已无 pending，兑现登记的 OPC 位/查询回复，并唤醒被 `*WAI` 暂停的队列。`isOperationPending()`（L403-L415）递归检查自身与全部子树。

**命令树自省**（[scpi.cpp:L432-L445](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/scpi.cpp#L432-L445)）——`createCommandList` 深度优先遍历：每个命令按 `queryable()`/`executable()` 各出一行（查询带 `?`），子节点递归时前缀累加 `leafName() + ":"`。`*LST?` 的输出就是这份全文清单。

#### 4.3.4 代码实践：错误注入预测表

1. **实践目标**：把"错误即字符串"的约定变成可查的表格，远程调试时能立刻对号入座。
2. **操作步骤**（源码推导，可随后本地验证）：
   - 对下表每个输入，先自己写出预期返回字符串与 SESR 置位，再对照 scpi.cpp 核对：

     | 发送 | 预期返回 | 置位 | 依据 |
     |---|---|---|---|
     | `:VNA:NOSUCHcmd 1` | `ERROR` | CME | parse 两层都找不到段名（scpi.cpp:568） |
     | `*RST?` | `QUERY_ERROR` | CME | `*RST` 只注册了 cmd 回调（appwindow.cpp:533；scpi.cpp:640-643） |
     | `:VNA:FREQuency:START abc` | `CMD_ERROR` | CME | cmd 回调 `paramToULongLong` 失败返回 ERROR，execute 升级（vna.cpp:1468-1470；scpi.cpp:632-635） |
     | `:VNA:FREQuency:START 1000000` | （无输出） | 无 | Empty 静默 |
     | `:VNA:FREQuency:START?` | `1000000` 之类数字 | 无 | query 回调返回数据，emit output |
     | 发完上面任一错误后 `*ESR?` | `32`（CME=0x20） | 读取后清零 | scpi.cpp:215-220、37-41 |

3. **需要观察的现象**：错误命令本身也会把错误字符串发回客户端（`emit output` 分支），所以**客户端能直接看到 `ERROR`/`CMD_ERROR` 字样**；`*ESR?` 是第二重确认。
4. **预期结果**：一张与实测一致的注入表。若某行实测与推导不符，优先复查该命令的注册名拼写与是否发生 nameCollision 静默失败（4.1.3）。
5. **待本地验证**：表为源码推导产物，务必在第 5 节搭好的环境里跑一遍。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `*ESR?` 的实现里读取后要 `SESR = 0x00`？
**答案**：IEEE 488.2 规定事件寄存器"读即销毁"（read-and-clear），防止同一错误被重复消费。若不清零，客户端每次查询都会看到历史错误的累积，无法区分"新错误"与"旧错误"。

**练习 2**：`*OPC?` 在有操作挂起时为什么不立即回复？
**答案**：`*OPC?` 的语义是"所有挂起操作完成时回 1"。立即回复就变成了"报告当前状态"。所以实现登记 `OPCQueryScheduled`，等 `someOperationCompleted()` 确认全树无 pending 后补发 `output("1")`（scpi.cpp:53-64、244-247）。

**练习 3**：一个查询回调想返回数据 `"ERROR"`（假设某仪器真有名为 ERROR 的状态），会怎样？
**答案**：灾难——`process()` 会把它当错误字符串处理：置 CME 位且**不发给客户端**（scpi.cpp:215-220）。保留字被协议占用，业务回调必须避开 `""`、`ERROR`、`CMD_ERROR`、`QUERY_ERROR`、`EXEC_ERROR`，以及（作为 bool 查询结果时）`TRUE`/`FALSE`。

## 5. 综合实践：注册 `:DEMO:HELLO?` 并验证

把本讲三个模块串成一个闭环：**注册（4.1）→ 解析命中（4.2）→ 验证与自省（4.3）**。

### 5.1 实践目标

给 GUI 添加一条查询命令 `:DEMO:HELLO?`，返回 `WORLD`；并用 TCP 通道与 `*LST?` 证明它真的长在命令树上、能被解析器正确命中。

### 5.2 操作步骤

**第一步：注册命令**（只改一个文件，不碰源码逻辑——本实践按"学习性修改"执行，验证后可还原）。

在 [appwindow.cpp:L521-L537](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L521-L537) 的 `AppWindow::SetupSCPI()` 中，`*RST` 注册之后插入：

```cpp
// 示例代码（讲义新增，非项目原有）
scpi.add(new SCPICommand("DEMO:HELLO", nullptr, [](QStringList) -> QString {
    return "WORLD";
}));
```

三个要点：

- 名字带冒号 `DEMO:HELLO`，靠 4.1 讲的 `addInternal` 自动创建 `DEMO` 中间节点，无需手写 `new SCPINode("DEMO")`；
- 第一个回调传 `nullptr`——`:DEMO:HELLO`（无 `?`）将返回 `CMD_ERROR`，这本身就是 4.3 的验证素材；
- 返回 `"WORLD"` 不是保留字，会作为查询结果原样发回。

**第二步：编译**（沿用 u1-l3 的构建方式）：

```bash
cd Software/PC_Application/LibreVNA-GUI
qmake6 && make
```

**第三步：启动并连通**。SCPI TCP 服务器默认启用、端口 19542（preferences.h:387-388）；也可用 `--port` 显式指定（appwindow.cpp:111-118）。无硬件也能做本实践，加 `--no-gui` 更省事：

```bash
./LibreVNA-GUI --no-gui --port 19542 &
```

**第四步：验证三连**（TCP 细节属下一讲 u10-l2，这里先用起来）：

```bash
# 1) 身份查询：证明链路通
printf '*IDN?\n' | nc -q 2 localhost 19542

# 2) 树自省：证明新命令已挂上（输出里应能找到 :DEMO:HELLO? 一行）
printf '*LST?\n' | nc -q 2 localhost 19542 | grep -i demo

# 3) 命中新命令：证明解析器逐层下降成功
printf ':DEMO:HELLO?\n' | nc -q 2 localhost 19542
```

（`-q 2` 让 nc 在关闭 stdin 后多等 2 秒收回应答，避免连接先断收不到回复；不同发行版参数略有差异。）

### 5.3 需要观察的现象与预期结果

| 命令 | 预期返回 |
|---|---|
| `*IDN?` | `LibreVNA,LibreVNA-GUI,<序列号或空>,<版本>` 一行 |
| `*LST?` | 数百行命令清单，其中含 `:DEMO:HELLO?`（queryable 有 `?` 行；executable 因 fn_cmd 为空只出查询行） |
| `:DEMO:HELLO?` | `WORLD` |
| `:DEMO:HELLO`（追加实验） | `CMD_ERROR` |
| `:demo:hello?`（追加实验） | `WORLD`（大小写无关，4.2 结论） |

### 5.4 如果没有可用的运行环境

改用源码阅读型验证：在 `SetupSCPI()` 插入上述代码后**不编译**，改走两步推演——

1. 手工执行 `addInternal("DEMO:HELLO", 0)` 的伪代码（4.1.2），确认会创建 `DEMO` 节点并在其下挂命令、无 nameCollision；
2. 手工执行 `parse(":DEMO:HELLO?")`（4.2.2），确认两层下降 + `?` 剥离 + query 回调命中，返回 `WORLD` 走 `emit output` 分支。

并在文档中标注「待本地验证」。

## 6. 本讲小结

- SCPI 命令树 = **根 `SCPI`（继承 `SCPINode`）→ `SCPINode` 目录 → `SCPICommand` 叶子**；每个叶子挂两个独立回调：`fn_cmd`（设置）与 `fn_query`（查询），任一可为 `nullptr` 表达"不可设置/不可查询"。
- 注册即挂树：名字带冒号会由 `addInternal` 自动创建中间节点；`nameCollision` 用长短形式匹配查重，冲突时**静默拒绝**（仅 qWarning）——命令"消失"先查这里。`Mode` 基类构造时把每个模式以自身 SCPI 名（如 `VNA`）挂到根，与激活状态无关。
- 长短助记符匹配只需一个函数：短形式 = 注册名砍掉尾部小写字母；输入侧先 `toUpper()`，所以客户端大小写无关，但**比短形式更短的拼写必不命中**（`START` 全大写 ⇒ `STA` 无效）。
- 解析是**队列化 + 递归下降**：`SCPI::input` 入队、`process()` 串行消化；`SCPINode::parse` 每层吃一段冒号前的名字；`;` 子命令无 `:`/`*` 前缀时从 `lastNode` 续接相对路径。
- 错误约定是"**错误即保留字符串**"：回调返回 `ERROR` 会被按调用形态升级为 `CMD_ERROR`/`QUERY_ERROR`；`process()` 据此置 SESR 的 CME/EXE 位，查询数据则原样 `emit output` 发回。`*ESR?` 读即清零。
- 同步与自省：`setOperationPending` 沿树上报驱动 `*OPC`/`*WAI` 状态机；非标准的 `*LST?` 遍历全树列出所有命令，是探索命令面的第一工具。

## 7. 下一步学习建议

本讲只讲清了"框架"——命令树的骨骼与文本协议的解析。下一讲 **u10-l2《SCPI 集成与 TCP 远程控制》**将补上另一半：

- `TCPServer` 的事件循环、多客户端处理与按行切帧（本讲只用了 `received → input`、`output → send` 两根连线）；
- 各模式 `createSCPI`/`SetupSCPI` 导出的具体命令命名空间（`:VNA:`、`:SA:`、`:SG:` 各自的命令面全貌）；
- 用 `nc`/`telnet` 完成一次"查询识别 → 设置中心频率 → 读回"的完整会话。

巩固本讲的建议阅读路线：先读 [spectrumanalyzer.cpp:L952 起](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L952) 的 SA 版 `FREQuency` 节点与 vna.cpp 对照，体会"同一框架、两种命令面"；再读 [portextension.cpp:L29-L33](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/Deembedding/portextension.cpp#L29-L33) 里四个 `add*Parameter` 一行式的参数注册，体会框架封装的威力。
