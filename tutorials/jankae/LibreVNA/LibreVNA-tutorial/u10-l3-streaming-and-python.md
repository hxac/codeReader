# 流式数据服务与 Python 自动化

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LibreVNA-GUI 提供的**两条脚本化数据通道**——SCPI 问答式「拉」与 StreamingServer 推送式「流」——各自的适用场景与局限。
2. 准确描述流式服务器输出的数据格式：逐行 JSON（NDJSON）、五个独立通道（VNA Raw/Calibrated/Deembedded、SA Raw/Normalized）、默认端口号与默认关闭的事实。
3. 读懂 `libreVNA.py` 这个官方 Python 客户端库：连接、`cmd`/`query` 原语、`*ESR?` 错误位检查、`SocketStreamReader` 的粘包处理，以及 `add_live_callback` 背后的流式线程。
4. 理解 Integrationtests 如何用 `unittest` 组织「自己拉起无头 GUI → SCPI 控制 → 断言 → 善后」的完整测试闭环。
5. 独立编写一个约 20 行的自动化取数脚本（含无硬件的 dry-run 分支）。

## 2. 前置知识

本讲是远程控制单元的最后一讲，建立在 u10-l1、u10-l2 之上。开始前请确认理解以下概念，不熟悉的术语都在前两讲出现过：

- **SCPI 问答模型**：LibreVNA-GUI 在 TCP 端口 19542 上提供 SCPI 服务；设置命令成功时**没有应答**，查询命令（以 `?` 结尾）返回一行文本；错误不回传报文，只置 IEEE 488.2 的 `*ESR?` 状态位（0x20 命令错、0x10 执行错、0x08 设备错、0x04 查询错）。
- **拉（pull）与推（push）**：「拉」是客户端主动发 `:VNA:TRACE:DATA? S11` 之类的查询、等 GUI 应答——一次一问，适合取整条扫描结果；「推」是 GUI 在每个测量点产生时主动写到一条 TCP 连接上——客户端只管收，适合实时监视、边扫边处理。
- **NDJSON（换行分隔 JSON）**：把每个 JSON 对象压成一行、以 `\n` 结尾的文本流格式。TCP 是字节流协议，本身没有「消息」边界，NDJSON 用换行符充当边界，是解决粘包/半包最简单的办法（u10-l2 讲过 SCPI 的 TCP 服务器用 `canReadLine()` 做同样的事）。
- **Python 基础**：`socket` 模块的 TCP 客户端、`threading.Thread`、`json.loads`，以及标准库 `unittest` 的 `setUp`/`tearDown`/`assertEqual` 三件套。
- **数据单位**（来自 u3-l1、u7-l2）：VNA 的测量值是线性复数（实部/虚部），S 参数幅值换算为 dB 用 \( 20\lg|S| \)；SA 的测量值是线性电压，注释明确规定「1.0 即 0 dBm」，换算为 dBm 同样是 \( 20\lg V \)。

一个需要当面澄清的误区：本手册大纲里把这路输出描述为「二进制流」，但**源码事实是逐行 JSON 文本流**（下文 [streamingserver.cpp:23-46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L23-L46) 用 `nlohmann::json::dump()` 序列化后追加 `'\n'`）。它与 SCPI 的区别不在「二进制 vs 文本」，而在「推送 vs 问答」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Software/PC_Application/LibreVNA-GUI/streamingserver.cpp/.h` | 流式服务器本体：监听端口、维护客户端集合、把测量点序列化为一行 JSON 广播 |
| `Software/PC_Application/LibreVNA-GUI/appwindow.cpp` | 创建/重建五个流式服务器（124-138、820-836），`addStreamingData()` 按数据级别分发（874-899） |
| `Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp` | VNA 模式数据管线上的三个「取样口」：Raw/Calibrated/Deembedded（1036、1058-1068） |
| `Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp` | SA 模式的两个取样口：Raw/Normalized（544、571-578） |
| `Software/PC_Application/LibreVNA-GUI/preferences.h` | 五个通道的默认开关与默认端口（389-398） |
| `Software/Integrationtests/tests/libreVNA.py` | 官方 Python 客户端库（注意：这是指向 `Documentation/UserManual/SCPI_Examples/libreVNA.py` 的**符号链接**） |
| `Software/Integrationtests/tests/TestBase.py` | 集成测试基类：拉起无头 GUI、建立 SCPI 连接、失败取证、优雅退出 |
| `Software/Integrationtests/tests/TestVNASweep.py` | VNA 扫描集成测试：SCPI 配扫描 → `*WAI` 等完成 → 断言数据 |
| `Software/Integrationtests/Integrationtest.py` | 测试入口：按固定顺序装载全部测试模块 |
| `Documentation/UserManual/SCPI_Examples/capture_live_data.py` | 官方流式取数示例（`add_live_callback` 的唯一使用样例） |
| `Documentation/UserManual/SCPI_Examples/retrieve_trace_data.py` | 官方问答式取数示例（对照组） |

## 4. 核心概念与源码讲解

### 4.1 StreamingServer 协议

#### 4.1.1 概念说明

SCPI 是「遥控器」：你按一下（发命令），仪器动一下（或答一句话）。但它有两个天生短板：

1. **数据要自己搬**：想知道扫描结果，得反复轮询 `:VNA:ACQ:FIN?`，再发 `:VNA:TRACE:DATA?` 把整条迹线一次性拉回来。想看「扫到第 137 个点时的实时值」就很别扭。
2. **拿不到中间处理级别**：`TRACE:DATA?` 返回的是 Trace 里最终的数据，而 GUI 内部其实同时维护着「原始/已校准/已去嵌入」多个版本（u9-l4 讲过 Trace 的双数据集）。

StreamingServer 就是为补这两点而生的旁路出口：GUI 在测量管线上凿出「取样口」，每个测量点一产生，就把它序列化成一行 JSON、写到所有已连接的 TCP 客户端上。它是**纯单向广播**——客户端只收不发，没有任何应答或命令语义，控制仍走 SCPI 那条 19542 端口。

#### 4.1.2 核心流程

以 VNA 模式为例，数据从设备到 Python 脚本的完整通路：

```text
设备(USB/TCP) ──原始包──> librevnadriver ──信号──> VNA::NewDatapoint()
                                                        │ 平均(多圈)
                                                        ▼
                              ① window->addStreamingData(Raw)          ──> 端口 19000
                                                        │ cal.correctMeasurement()
                                                        ▼
                              ② addStreamingData(Calibrated)  (仅校准激活时)──> 端口 19001
                                                        │ deembedding.Deembed()
                                                        ▼
                              ③ addStreamingData(Deembedded)(仅去嵌入激活时)──> 端口 19002
```

SA 模式对应两口：Raw（端口 19100）与 Normalized（端口 19101）。

服务器侧每收到一个点的处理逻辑（伪代码）：

```text
addData(测量点 m, 是否零扫宽):
    j = { pointNum, (零扫宽 ? time : frequency[+dBm]), Z0(仅VNA),
          measurements: { 键_real: 实部, 键_imag: 虚部 } }   # SA 则直接 { 键: 线性电压 }
    一行文本 = j.dump() + '\n'
    for 每个 已连接且 isOpen 的 socket: write(一行文本)
```

要点：

- **每点一帧、帧即一行**，客户端按 `\n` 切帧即可。
- **五个通道是五个独立服务器**（各自一个 `QTcpServer`、一个端口），互不干扰；想同时要 Raw 和 Calibrated 就开两条连接。
- **通道默认全部关闭**（`preferences.h` 中五个 enabled 默认值均为 `false`），必须先在 Preferences 对话框的 Streaming Servers 页里勾选启用。

#### 4.1.3 源码精读

**服务器本体**。构造函数监听端口并维护一个 socket 集合——注意与 SCPI 的 `TCPServer`（单连接、后来者顶替）不同，这里用 `std::set` 容纳**多个并发客户端**，断连的 socket 在状态变化回调里移除并 `deleteLater()`：

- [streamingserver.cpp:L5-L21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L5-L21)：`StreamingServer::StreamingServer(int port)` 监听任意地址，新连接插入集合，并在 `UnconnectedState` 时清理。

VNA 数据的序列化是本协议的核心，全文不过 24 行：

- [streamingserver.cpp:L23-L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L23-L46)：`addData(const DeviceDriver::VNAMeasurement&, bool is_zerospan)` 把测量点转为 JSON 并广播。三个细节值得逐行看：
  - `pointNum` 永远输出——它是扫描内点号（从 0 起），客户端靠它判断扫描进度；
  - 零扫宽与非零扫宽共用一个 union（u3-l1 讲过 `frequency/dBm` 与 `us` 共存内存），所以这里用 `is_zerospan` 分流：零扫宽输出 `time`（`us * 0.000001`，即毫秒换成秒），非零扫宽输出 `frequency` 与 `dBm`；
  - JSON 不支持复数，`std::complex` 被拆成 `S11_real` / `S11_imag` 两个键（第 35-38 行的循环）。

- [streamingserver.cpp:L48-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L48-L68)：`addData(const DeviceDriver::SAMeasurement&, bool is_zerospan)` 是 SA 版本：没有 `Z0`/`dBm`，measurements 的值是单个 double（线性电压，1.0 即 0 dBm，见 [devicedriver.h:L375-L393](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L375-L393) 的注释），键是端口名（如 `PORT1`）。

**AppWindow 侧的接线**。五个服务器指针是 AppWindow 的成员，构造函数按偏好创建：

- [appwindow.cpp:L124-L138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L124-L138)：五个 `if(p.StreamingServers.XXX.enabled)` 分支分别 new 出 VNA Raw/Calibrated/Deembedded、SA Raw/Normalized 服务器。
- [appwindow.cpp:L820-L836](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L820-L836)：`PreferencesChanged` 槽里的 `updateStreamingServer` lambda——偏好一变（开关、端口）就销毁重建对应服务器，这就是「在 Preferences 里改端口立即生效」的实现。
- [appwindow.cpp:L874-L899](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L874-L899)：`AppWindow::addStreamingData()` 的两个重载，按 `VNADataType`/`SADataType` 枚举把测量点路由到对应服务器；服务器为空（通道关闭）时是零开销的空指针检查。

**取样口的位置**（这一段与 u7-l4 的数据分级相衔接）：

- [vna.cpp:L1036](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1036)：平均完成后立刻送出 Raw——即「已平均、未校准」的数据。
- [vna.cpp:L1058-L1062](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1058-L1062)：`cal.correctMeasurement(m_avg)` **就地修改**测量点后，若校准类型非 None 才送出 Calibrated。
- [vna.cpp:L1064-L1069](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1064-L1069)：去嵌入激活时再 `deembedding.Deembed(m_avg)` 一次、送出 Deembedded。注意 1058 行的校准修改的是同一个 `m_avg` 对象——所以三个通道输出的分别是「同一数据经受不同深度处理」的三个快照。
- [spectrumanalyzer.cpp:L544](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L544) 与 [spectrumanalyzer.cpp:L571-L578](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L571-L578)：SA 的两口，Normalized 在逐点除以归一化参考并缩放到目标电平之后输出。

**默认端口与开关**（背下这张表，写客户端时不用翻代码）：

| 通道 | 偏好键（`preferences.h` 描述表） | 默认开关 | 默认端口 |
| --- | --- | --- | --- |
| VNA Raw | `StreamingServers.VNARawData` | 关 | 19000 |
| VNA Calibrated | `StreamingServers.VNACalibratedData` | 关 | 19001 |
| VNA Deembedded | `StreamingServers.VNADeembeddedData` | 关 | 19002 |
| SA Raw | `StreamingServers.sARawData` | 关 | 19100 |
| SA Normalized | `StreamingServers.SANormalizedData` | 关 | 19101 |

出处：[preferences.h:L389-L398](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L389-L398)（SettingDescription 描述表，u2-l3 讲过它的点分路径机制）。 Preferences 对话框中的 Streaming Servers 页（`preferencesdialog.ui` 里名为 `StreamingServers` 的页）逐项绑定这些复选框与端口号，见 [preferences.cpp:L353-L362](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L353-L362)。

**帧格式与单位换算**。一行典型的 VNA Raw 数据（非零扫宽）形如：

```json
{"Z0":50.0,"dBm":-10.0,"frequency":1000000000.0,"measurements":{"S11_imag":-0.0123,"S11_real":0.2031},"pointNum":42}
```

拿到线性复数后自己换算：

\[ |S_{11}|_{\mathrm{dB}} = 20\lg\sqrt{\mathrm{re}^2 + \mathrm{im}^2} \]

SA 通道的线性电压 \( V \)（1.0 ↔ 0 dBm）换算为：

\[ P_{\mathrm{dBm}} = 20\lg V \]

#### 4.1.4 代码实践

**实践目标**：亲眼看到一行行 JSON 从流式端口涌出来，验证「逐点推送 + 换行分帧 + `_real`/`_imag` 拆键」三件事。

**操作步骤**（需要有 LibreVNA 硬件并连上 GUI；纯源码阅读替代方案见第 5 节 dry-run 分支）：

1. 启动 LibreVNA-GUI，连接设备。
2. 菜单 Window → Preferences → Streaming Servers 页，勾选「VNA raw data: Enabled」，确认端口为 19000，确定保存。
3. 在终端连接该端口并观察输出（Ctrl+C 退出）：

```bash
nc localhost 19000
```

4. 让 GUI 处于 VNA 模式并运行扫描，观察终端。

**需要观察的现象**：

- 每个测量点滚出一行 JSON，`pointNum` 从 0 递增到点数-1 后回绕（新扫描开始）。
- 行内同时有 `frequency`、`dBm`、`Z0` 与若干 `<名>_real`/`<名>_imag` 键对。
- 换到零扫宽（点频）模式后，`frequency`/`dBm` 消失，代之以 `time`。

**预期结果**：上述现象与 [streamingserver.cpp:L23-L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L23-L46) 的字段逐项对应。**待本地验证**（本环境无硬件，以上基于源码推导，未实际运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 VNA 通道的 JSON 里每条测量要拆成 `_real`/`_imag` 两个键，而 SA 通道不用？

**答案**：JSON 标准没有复数类型。VNA 的 `VNAMeasurement::measurements` 值是 `std::complex<double>`（见 [devicedriver.h:L294-L297](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L294-L297)），必须拆成实虚两个 double 才能序列化；SA 的 `SAMeasurement::measurements` 值本身就是 `double`（线性电压），单键即可。

**练习 2**：两个 Python 脚本同时 `nc localhost 19000`，都能收到数据吗？和 SCPI 端口 19542 的行为有何不同？

**答案**：能。StreamingServer 用 `std::set<QTcpSocket*>` 保存所有客户端并对每个 `isOpen()` 的 socket 广播（[streamingserver.cpp:L41-L45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/streamingserver.cpp#L41-L45)）；而 SCPI 的 TCPServer 是单连接、新连接顶替旧连接（u10-l2）。这是「广播数据」与「独占会话」两种语义的自然差异。

**练习 3**：客户端收到的 SA 数值是 0.5（线性电压），对应多少 dBm？

**答案**：\( 20\lg 0.5 \approx -6.02 \) dBm。因为定义 1.0 ↔ 0 dBm，线性电压每减半就是 −6 dB。

### 4.2 libreVNA.py 测试库

#### 4.2.1 概念说明

`libreVNA.py` 是官方维护的唯一 Python 客户端，一个文件、无第三方依赖（只用标准库 socket/threading/json/re）。先看一个仓库组织上的巧思：

```text
Software/Integrationtests/tests/libreVNA.py -> ../../../Documentation/UserManual/SCPI_Examples/libreVNA.py
```

`tests` 目录下这份只是**符号链接**，真身在 `Documentation/UserManual/SCPI_Examples/`。集成测试与用户示例共享同一份客户端代码——改一处，两边同步，杜绝「测试用的库和教给用户的库不一致」。

这个类身兼两职：

1. **SCPI 客户端**：`cmd()` 发设置命令、`query()` 发查询读一行应答、`get_status()` 读 `*ESR?` 把错误位翻译成异常。
2. **流式客户端**：`add_live_callback(port, callback)` 为某个流式通道开一条新连接和一个后台线程，把每行 JSON 解析成 dict（复数已拼回 Python 的 `complex`）回调给你。

#### 4.2.2 核心流程

**命令-应答的守门流程**：

```text
cmd(命令)
  ├── sendall(命令 + '\n')            # 设置命令本身没有应答
  └── (默认开启检查)
      └── get_status() = query("*ESR?")
            ├── 应答必须是纯数字，否则抛异常
            └── status & 0x20 → "Command Error"    （命令不存在/语法错）
                status & 0x10 → "Execution Error"  （参数无法执行）
                status & 0x08 → "Device Error"     （设备侧出错）
                status & 0x04 → "Query Error"
```

这正是 u10-l1「错误即状态位」约定的 Python 端兑现：每个命令后主动读一次 `*ESR?`（读即清零），把静默错误变成 Python 异常。

**读行的粘包处理**：TCP 是字节流，一次 `recv` 可能含半行、一行或三行数据。`SocketStreamReader` 维护一个 `_recv_buffer` 残留缓冲：先把缓冲灌进目标视图，不够再补一次非阻塞 `recv_into`；`readuntil(b"\n")` 在缓冲里找分隔符，找到则返回「含换行」的完整行、剩余字节留存给下一次调用；超时（默认 1 秒，可用 `timeout` 参数放宽）抛异常。这与 GUI 端 `TCPServer` 的 `canReadLine()` 是同一问题在两端的两种解法。

**流式线程模型**：

```text
add_live_callback(19000, cb)
  ├── 该端口还没有线程？ ──是──> 新开一条 TCP 连接 + threading.Thread(__live_thread)
  └── 已有 ──> 回调列表追加 cb（多订阅者共享一条连接）

__live_thread 循环：
  line = reader.readline()          # 超时 0.1s，异常吞掉继续
  data = json.loads(line)
  若 "Z0" in data：                 # VNA 数据的判别标志
      把 <名>_real/<名>_imag 拼回 complex
  for cb in 回调列表: cb(data)
```

判别 VNA/SA 数据的方式很朴素：VNA 帧有 `Z0` 键，SA 帧没有。另外注意一个已知的小瑕疵：`remove_live_callback` 里判空用的是 `len(self.live_callbacks) == 0`（整个字典为空）而不是 `len(self.live_callbacks[port]) == 0`（[libreVNA.py:L143-L150](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L143-L150)），单端口清空时线程可能不会如期 join——读代码时留意这类「文档行为」与注释意图的偏差。

#### 4.2.3 源码精读

- [libreVNA.py:L73-L89](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L73-L89)：构造函数。默认连 `localhost:19542`（SCPI 默认端口），连不上给出「确认 GUI 在运行且 TCP server 已启用」的提示；`__del__` 里关 socket。
- [libreVNA.py:L94-L109](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L94-L109)：`cmd()`——发命令 + 换行，默认（`check_cmds=True`）读 `*ESR?` 检错。返回值是原始状态字节，调用方可借此读取其它位。
- [libreVNA.py:L111-L124](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L111-L124)：`query()` 发查询读一行；`get_status()` 用正则 `^\d+$` 校验 `*ESR?` 应答并限制在 0..255。
- [libreVNA.py:L8-L71](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L8-L71)：`SocketStreamReader` 全文。重点读 `readuntil()`（L31-L59，找分隔符 + 超时 + 残留回填）与 `_recv_into()`（L61-L71，非阻塞 socket + 缓冲优先）。
- [libreVNA.py:L126-L150](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L126-L150)：`add_live_callback()`/`remove_live_callback()`——每个流式端口一条专属连接一个线程，多回调共享。
- [libreVNA.py:L152-L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L152-L177)：`__live_thread()`——逐行解析、按 `Z0` 判别 VNA、`_real`/`_imag` 拼回 `complex(real, imag)`、分发给所有回调；异常（如 0.1 秒读超时）静默吞掉继续循环。
- [libreVNA.py:L180-L194](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L180-L194)：`parse_VNA_trace_data()`——把 `:VNA:TRACE:DATA?` 的应答（形如 `[f1,re1,im1,f2,re2,im2,...]` 的字符串）解析成 `(频率, complex)` 元组列表；先剥掉方括号再按逗号切，并校验个数是 3 的倍数。L196-L209 的 `parse_SA_trace_data()` 同理，元组是 `(频率, dBm)`、个数须为 2 的倍数。
- 官方示例对照：[retrieve_trace_data.py:L34-L48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/UserManual/SCPI_Examples/retrieve_trace_data.py#L34-L48) 是问答式（轮询 `:VNA:ACQ:FIN?` 后一次 `TRACE:DATA?`）；[capture_live_data.py:L38-L53](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/UserManual/SCPI_Examples/capture_live_data.py#L38-L53) 是流式（回调里检查 `data["pointNum"] == 500` 判末点，`remove_live_callback(19000, callback)` 收线）。两个文件并排读，就是本讲两种路线的最小对照实验。

#### 4.2.4 代码实践

**实践目标**：用 `libreVNA.py` 的 API 走通「连接 → 校验身份 → 配扫描 → 取数」骨架，并让脚本在无硬件时也能跑（dry-run 只打印将发送的 SCPI 命令）。完整版见第 5 节综合实践，这里先做一个 20 行的最小版。

**操作步骤**：

1. 把 `Documentation/UserManual/SCPI_Examples/` 加入工作目录（`libreVNA.py` 就在其中）。
2. 新建 `mini_capture.py`，内容如下（**示例代码**，非仓库原有文件）：

```python
#!/usr/bin/env python3
import sys, time
from libreVNA import libreVNA

DRY = "--dry" in sys.argv          # 无硬件时用 --dry 演习
vna = None

def C(cmd):                        # 统一出口：dry-run 只打印
    if DRY: print("SCPI>", cmd); return
    vna.cmd(cmd)

if not DRY:
    vna = libreVNA('localhost', 19542)
    print(vna.query("*IDN?"))      # 应答含 "LibreVNA-GUI"
else:
    print("LibreVNA-GUI (dry-run, 未连接)")

C(":DEV:MODE VNA"); C(":VNA:SWEEP FREQUENCY")
C(":VNA:STIM:LVL -10"); C(":VNA:ACQ:IFBW 10000")
C(":VNA:ACQ:AVG 1");  C(":VNA:ACQ:POINTS 501")
C(":VNA:FREQuency:START 1000000"); C(":VNA:FREQuency:STOP 6000000000")

if not DRY:
    while vna.query(":VNA:ACQ:FIN?") == "FALSE":
        time.sleep(0.1)            # 轮询等待扫描完成
    S11 = vna.parse_VNA_trace_data(vna.query(":VNA:TRACE:DATA? S11"))
    for f, s in S11[:3]:
        print(f, abs(s))           # 线性幅值，dB 用 20*lg|s|
```

3. 无硬件先跑 `python3 mini_capture.py --dry`，确认命令序列与你从 [TestVNASweep.py:L10-L18](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/TestVNASweep.py#L10-L18) 读到的测试序列一致；有硬件再去掉 `--dry` 实测。

**需要观察的现象**：dry-run 模式逐行打印 8 条 SCPI 命令；实跑模式先打印 IDN，约数秒后打印前三个点的频率与线性幅值。

**预期结果**：501 点扫描完成后，`S11[0][0]` 为 1000000、`S11[-1][0]` 为 6000000000（与 [TestVNASweep.py:L21-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/TestVNASweep.py#L21-L23) 的断言一致）。**待本地验证**（本环境无 GUI 进程与硬件，未实际运行）。

#### 4.2.5 小练习与答案

**练习 1**：`vna.cmd(":DEV:MODE VNA")` 执行后，客户端怎么知道命令有没有被接受？

**答案**：设置命令无应答。`cmd()` 默认 `check_cmds=True`，发完立刻 `query("*ESR?")`：若返回字节含 0x20（命令错）或 0x10（执行错）等位就抛异常（[libreVNA.py:L94-L109](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L94-L109)）。`*ESR?` 读即清零，所以每条命令的检查互不串扰。

**练习 2**：`__live_thread` 里为什么要 `try/except` 把异常「吞掉」？

**答案**：`readline()` 默认超时 0.1 秒，没有新数据时抛超时异常是**常态而非错误**（扫描暂停、通道空闲）。若不捕获，线程会在第一次空闲时退出，流式订阅就静默失效了。代价是真正的解析错误也被吞——这是「保持线程活着」与「错误可见」之间的取舍（[libreVNA.py:L152-L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L152-L177)）。

**练习 3**：SCPI 连接与流式连接能共用一个 socket 吗？

**答案**：不能。SCPI 是 19542 上的问答会话（单连接、后来者顶替），流式是 19000 等端口上的单向广播。`add_live_callback` 为流式另开一条 socket 连接（[libreVNA.py:L128-L138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/libreVNA.py#L128-L138)），控制面与数据面物理分离——这也是把 SCPI 服务器与 StreamingServer 做成两个类的根本原因。

### 4.3 测试用例组织

#### 4.3.1 概念说明

`Software/Integrationtests/` 是一套「**把整个 GUI 当被测对象**」的端到端测试：不是调用某个函数断言返回值，而是亲手把 GUI 作为子进程拉起来（无头模式），通过 SCPI 像真实用户一样操作它，再用断言验证仪器行为。它测的是从 SCPI 命令树 → 模式设置结构 → 驱动 → USB 协议 → 固件的**全链路**，因此需要连接真实硬件——这也是它与 `LibreVNA-Test` 单元测试工程（u11-l1）的根本区别。

三层结构：

- `Integrationtest.py`：入口，按固定顺序装载测试模块；
- `TestBase.py`：公共基类，负责 GUI 进程生命周期与取证；
- `TestXXX.py`：每个文件一组同领域用例，全部继承 `TestBase`。

#### 4.3.2 核心流程

每个用例的执行时序（unittest 框架驱动）：

```text
setUp():                          # 每个测试方法前各跑一次
  1. Popen(GUI, ['-p','19544','--reset-preferences','--no-gui','-platform','offscreen'])
  2. 轮询 log.txt 等待 "Listening on port 19544"（3 秒超时）
  3. libreVNA('localhost', 19544, timeout=4) 建立 SCPI 连接
  4. *CLS 清状态；:DEV:CONN? 确认已连设备，否则直接失败

test_xxx():                       # 你写的用例体
  cmd(...) 配置 → 查询/断言

tearDown():                       # 每个测试方法后各跑一次
  1. 判定本用例是否通过
  2. 失败则 query(":DEV:PACKETLOG?") 存 packetlog_时间戳.vnalog（u4-l3 的包日志）
  3. 向 GUI 发 SIGINT → 等 3 秒 → 仍不退出则 kill
  4. 通过且未崩溃：删 log.txt；否则改名为 errorlog_时间戳.txt 留档
  5. 若 GUI 崩溃：抛 "GUI crashed"
```

几个设计要点：

- **`-p 19544`**：故意不用默认 19542，避免与开发者自己开着的 GUI 服务器撞端口。
- **`--reset-preferences`**：每次用默认偏好起步，保证测试可重复（也解释了为什么集成测试不碰流式通道——五个流式服务器默认全关）。
- **`--no-gui -platform offscreen`**：u2-l1 讲过的无头分流 + Qt 离屏平台，CI 上无需显示器。
- **就绪探测靠日志**：GUI 启动是异步的，SCPI 端口何时可连没有事件可订阅，于是轮询 stdout 日志里的 `Listening on port`（该行来自 [tcpserver.cpp:L8](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/tcpserver.cpp#L8) 的 `qInfo()`）。
- **SIGINT 优雅退出**：复用 u2-l1 讲过的信号处理链（SIGINT → `tryExitGracefully` → `closeEvent` 清理），3 秒兜底强杀。

#### 4.3.3 源码精读

- [TestBase.py:L11-L26](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/TestBase.py#L11-L26)：`setUp()` 全文。Popen 参数、日志轮询、SCPI 连接、`*CLS` 与 `:DEV:CONN?` 前置校验。`definitions.py` 里 `GUI_PATH = "../PC_Application/LibreVNA-GUI/LibreVNA-GUI"` 指定被测二进制，测试必须从 `Integrationtests/` 目录启动。
- [TestBase.py:L37-L73](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/TestBase.py#L37-L73)：`tearDown()` 全文。含一段按 Python 版本（3.4-3.10 vs 3.11+）取测试结果的兼容代码，之后是失败取证 → SIGINT → 日志归档 → 崩溃检测。
- [TestVNASweep.py:L5-L8](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/TestVNASweep.py#L5-L8)：`waitSweepTimeout()`——先断言 `:VNA:ACQ:FIN?` 为 `FALSE`（确认命令已生效、扫描在跑），再 `*WAI` 阻塞到操作完成（u10-l1 讲过的同步原语），最后断言 `FIN?` 变为 `TRUE`。三步缺一不可：只查一次 `FIN?` 无法区分「还没开始」与「已经完成」。
- [TestVNASweep.py:L10-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/TestVNASweep.py#L10-L23)：`test_sweep_frequency()`——标准用例范本：8 条 `cmd()` 配置 → `waitSweepTimeout(2)` → `parse_VNA_trace_data(query("::VNA:TRACE:DATA? S11"))` → 断言首末频率。同文件的 `test_sweep_zerospan()`（L25-L41）断言零扫宽时首点时间为 0、末点时间落在 0.1~0.5 秒（扫描时长本身成了被测对象）；`test_segmented_sweep()`（L73-L85）用 10001 点验证分段扫描（u7-l1）的点数不受单扫 4501 点限制。
- [Integrationtest.py:L4-L16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/Integrationtest.py#L4-L16)：模块装载顺序表。注释点明 `TestUpdate` 必须排第一——它会先把连接的 VNA 固件升级到待测版本，后续测试才有意义；末行 `exit(int(not result.wasSuccessful()))` 把测试结果转成进程退出码，供 CI 判定。
- [Integrationtest.py:L20-L34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/Integrationtest.py#L20-L34)：启动前清理上次运行遗留的 `errorlog_*`/`packetlog_*`，优先使用模块自定义 `suite()`、否则装载全部用例。

#### 4.3.4 代码实践

**实践目标**：跑通一条集成测试（或在其无硬件约束下完成等价的代码走读），并模仿 `TestVNASweep` 的范本写一个自己的用例。

**操作步骤**：

1. 编译 GUI 得到可执行文件（u1-l3），确认 `definitions.py` 里的 `GUI_PATH` 与之匹配。
2. 连接硬件后，从 `Software/Integrationtests/` 目录运行单个模块（**示例命令**）：

```bash
cd Software/Integrationtests
python3 -m unittest tests.TestVNASweep -v
```

3. 仿写用例：在 `tests/` 下新建文件（**示例代码**，非仓库原有文件）：

```python
from tests.TestBase import TestBase

class TestMySweep(TestBase):
    def test_narrow_span(self):
        self.vna.cmd(":DEV:MODE VNA")
        self.vna.cmd(":VNA:SWEEP FREQUENCY")
        self.vna.cmd(":VNA:ACQ:IFBW 10000")
        self.vna.cmd(":VNA:ACQ:AVG 1")
        self.vna.cmd(":VNA:ACQ:POINTS 101")
        self.vna.cmd(":VNA:FREQuency:START 2400000000")
        self.vna.cmd(":VNA:FREQuency:STOP 2500000000")
        self.vna.cmd("*WAI", timeout=2)
        S11 = self.vna.parse_VNA_trace_data(self.vna.query(":VNA:TRACE:DATA? S11"))
        self.assertEqual(len(S11), 101)
```

**需要观察的现象**：`-v` 模式逐用例打印 `ok`；人为写错一个断言（比如把 101 改成 102）再跑，应看到用例失败、目录下多出 `errorlog_*.txt` 与 `packetlog_*.vnalog` 取证文件。

**预期结果**：全部用例 `ok`，退出码 0；目录里不留 `log.txt`。**待本地验证**（需要硬件与已编译 GUI，本环境两者皆无）。

无硬件替代方案：把 `TestBase.py` 的 `setUp` 与 `TestVNASweep.py` 的任一用例并排通读，画出一幅「进程启动 → 端口就绪 → SCPI 会话 → 断言 → SIGINT 收尾」的时序图，标注每一步的代码行号——这是纯阅读也能完成的等价练习。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TestBase` 用 `-p 19544` 而不是默认的 19542？

**答案**：避免与开发者机器上已经在运行的 GUI 实例抢端口。`-p/--port` 是 [appwindow.cpp:L90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L90) 定义的命令行选项，允许每次测试会话使用独立端口，测试之间、测试与人工使用之间互不干扰。

**练习 2**：`waitSweepTimeout` 里第一条断言 `FIN? == "FALSE"` 有什么用？删掉它行不行？

**答案**：它证明扫描**确实启动过**。若配置命令因为某种原因没有触发设备重配，`FIN?` 可能从一开始就是 `TRUE`，后面的 `*WAI` 立即返回、断言照样通过——测试会静默漏检「扫描根本没跑」的回归。先断言 `FALSE` 把「扫描在跑」变成前置条件，用时序而非状态区分两种情况。

**练习 3**：`Integrationtest.py` 为什么把 `TestUpdate` 放在列表第一位？

**答案**：`TestUpdate` 负责把连接的设备固件升级到与待测 GUI 匹配的版本（协议版本协商见 u4-l2）。固件版本不对，后续所有测试的通信基础都不成立，所以它必须最先执行——这是用装载顺序表达的隐式依赖。

## 5. 综合实践

**任务**：写一个约 40 行的完整脚本 `s11_capture.py`，把本讲两条路线都走一遍——用 SCPI 配置并等待一次 501 点扫描（拉），同时订阅 19000 流式通道统计收到的点数（推），最后对比两边的数据量是否一致。无硬件时脚本以 `--dry` 演习模式打印将发送的全部命令与将要连接的端口。

**参考实现**（**示例代码**，非仓库原有文件；有硬件分支的行为依据 [TestVNASweep.py:L10-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/Integrationtests/tests/TestVNASweep.py#L10-L23) 与 [capture_live_data.py:L38-L53](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/UserManual/SCPI_Examples/capture_live_data.py#L38-L53)，dry 分支为自行设计）：

```python
#!/usr/bin/env python3
"""拉(SCPI问答) + 推(19000流式) 双路取 S11；无硬件用 --dry 演习。"""
import sys, time, math
from libreVNA import libreVNA

DRY, live, count = "--dry" in sys.argv, None, [0]
vna = None

def C(cmd):
    print("SCPI>", cmd)
    if not DRY: vna.cmd(cmd)

def on_point(data):                       # 流式回调：每点一次
    count[0] += 1
    s = data["measurements"].get("S11")   # 已由库拼回 complex
    if data["pointNum"] % 100 == 0:
        print(f"live #{data['pointNum']}: "
              f"{20*math.log10(abs(s)):.2f} dB @ {data['frequency']/1e9:.3f} GHz")

if not DRY:
    vna = libreVNA('localhost', 19542)    # 控制面
    print("IDN:", vna.query("*IDN?"))
    if vna.query(":DEV:CONN?") == "Not connected":
        sys.exit("未连接设备（先在 GUI 中连接，或用 --dry 演习）")
    vna.cmd(":VNA:ACQ:STOP")
    live = vna.add_live_callback(19000, on_point)   # 数据面：VNA Raw 通道
else:
    print("IDN: LibreVNA-GUI (dry-run, 未连接)")

C(":DEV:MODE VNA");  C(":VNA:SWEEP FREQUENCY")
C(":VNA:STIM:LVL -10"); C(":VNA:ACQ:IFBW 10000")
C(":VNA:ACQ:AVG 1");  C(":VNA:ACQ:POINTS 501")
C(":VNA:FREQuency:START 1000000"); C(":VNA:FREQuency:STOP 6000000000")

if not DRY:
    vna.cmd(":VNA:ACQ:RUN")
    while vna.query(":VNA:ACQ:FIN?") == "FALSE":
        time.sleep(0.1)
    S11 = vna.parse_VNA_trace_data(vna.query(":VNA:TRACE:DATA? S11"))
    print(f"SCPI 侧取回 {len(S11)} 点，流式侧收到 {count[0]} 点")
    print("首点 %.1f Hz，末点 %.1f Hz" % (S11[0][0], S11[-1][0]))
```

**验证要点**（有硬件时）：

1. 运行前在 Preferences → Streaming Servers 里启用 VNA raw data（默认关闭，端口 19000）。
2. SCPI 侧应取回 501 点，首点 1 MHz、末点 6 GHz。
3. 流式侧计数同样应为 501（若 GUI 在脚本连接前已扫过部分点，计数会按整圈回绕，可放宽为 `count[0] >= 501`）。
4. 同一点的两种读数可交叉验证：对同一频率，\( 20\lg|S_{11,\text{live}}| \) 与 SCPI 取回复数的换算结果应一致。

**待本地验证**：本环境既无 LibreVNA 硬件也无已编译 GUI，以上「有硬件」行为均未实际运行，dry 分支可直接运行验证命令序列。常见故障排查：`Unable to connect to LibreVNA-GUI` → GUI 未启动或 SCPI 服务器未启用；`Unable to connect to streaming server at port 19000` → 忘开 Streaming Servers 偏好；连上却收不到数据 → GUI 未处于运行中的 VNA 扫描。

## 6. 本讲小结

- 脚本化控制有**两条互补路线**：SCPI 问答（19542，请求-应答、可控制一切、适合取整条结果）与流式服务（19000-19102 五通道，单向广播、逐点推送、适合实时监视）；控制面与数据面物理分离。
- 流式输出是**逐行 JSON 文本**（NDJSON），不是二进制：每点一帧，VNA 帧含 `pointNum/Z0/(frequency,dBm|time)/<名>_real|_imag`，SA 帧含 `pointNum/(frequency|time)/<端口名>: 线性电压(1.0=0dBm)`；复数拆实虚两键是 JSON 无复数类型的权宜。
- 五个通道是五个**独立的 QTcpServer**，支持多客户端并发广播（与 SCPI 单连接顶替形成对比）；**默认全部关闭**，须在 Preferences → Streaming Servers 启用，改端口即时重建生效。
- 三个 VNA 取样口对应处理深度：Raw（已平均未校准）、Calibrated（仅校准激活时）、Deembedded（再去嵌入）——客户端按需订阅不同深度的同一数据。
- `libreVNA.py` 以符号链接方式同时服务于集成测试与用户示例（单一事实来源）；`cmd()` 靠 `*ESR?` 把静默错误变成异常，`SocketStreamReader` 用残留缓冲 + 换行分隔解决粘包，`add_live_callback` 每端口一线程并把 `_real/_imag` 拼回 `complex`。
- Integrationtests 是端到端测试的范本：`TestBase` 拉起无头 GUI（`-p 19544 --reset-preferences --no-gui -platform offscreen`）、轮询日志等端口就绪、失败时抓 `PACKETLOG` 取证、SIGINT 优雅收尾；`waitSweepTimeout` 的「先证 FALSE → `*WAI` → 再证 TRUE」三步是等待类断言的通用套路。

## 7. 下一步学习建议

本讲讲完，远程控制单元（u10）就此收官。接下来推荐：

1. **u11-l1 单元测试与数值验证**：从需要硬件的集成测试转向纯 PC 侧的 `LibreVNA-Test` 单元测试工程（parameters、calibration、FFT 等），体会两种测试粒度的互补；本讲的 `waitSweepTimeout` 三步套路与那里的已知答案断言一脉相承。
2. **阅读 `Documentation/UserManual/SCPI_Examples/` 其余示例**：`export_s11_s1p_csv.py`（含命令行参数处理与 Touchstone 写文件）、`S11_calibration.py`（纯脚本完成校准，串联 u9 的校准知识）、`deembedding_test.py`——它们是本讲脚本的天然进阶素材。
3. **通读 SCPI Programming Guide**（`Documentation/UserManual/ProgrammingGuide.pdf`，由仓库的 `.tex` 生成）：把 u10-l1/l2 的命令树知识与本讲的客户端原语对上号，形成完整的可脚本化命令面清单。
4. 若你对流式通道有二次开发想法，可尝试给 `StreamingServer` 增加「SA 帧补 `Z0` 类判别字段」或研究把 JSON 换成更紧凑编码的取舍——记得先用本讲的 `nc` 观察法建立基线，再动手。
