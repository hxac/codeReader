# 数据采集线程与帧组装

## 1. 本讲目标

上一篇（u8-l1）我们建立了 V7 GUI 的包结构与六标签页骨架，知道了数据最终要送到 `Main View` 标签页去画 Range-Doppler 图。但「USB 字节流」是怎么变成「一张 64×32 的雷达帧」的，我们一直是个黑盒。本讲就打开这个黑盒。

学完本讲，你应当能够：

- 说清「后台采集线程 + 有界队列 + 显示线程」这种生产者—消费者解耦模式为什么对实时雷达 GUI 不可或缺。
- 解释 USB 读取为何会切断数据包，以及「残差（residual）拼接」如何在不丢字节的前提下还原完整包。
- 复述 11 字节数据包的逐字段布局，并说明 `_ingest_sample` 如何用一维计数器 `_sample_idx` 把样本映射到二维 `[range, doppler]` 单元。
- 追踪一个 11 字节数据包从 `conn.read()` 到 `RadarFrame.range_doppler_i[rbin, dbin]` 的完整路径，并指出 2048 个样本凑齐后整帧入队的触发点。

## 2. 前置知识

在进入源码前，先用通俗语言对齐三个概念。

**USB 是「字节流」，不是「消息流」。** 我们调用一次 `read(4096)`，操作系统只会把 FPGA 那 8 位 FIFO 里现成的字节倒给我们，至于倒出来的 4096 字节是从哪个包的哪个字节开始、到哪里结束，USB 协议并不保证。一次读取里可能横跨「3 个完整数据包 + 1 个被腰斩的包」，也可能全是噪声。因此上位机必须自己做「成帧（framing）」。

**Range-Doppler 图是一张 64×32 的矩阵。** FPGA 对每个距离门×多普勒门（range × doppler）的交叉点输出一对 I/Q 复数样本和一个 CFAR 检测位，共 \(64 \times 32 = 2048\) 个单元。FPGA 按固定顺序把这 2048 个单元逐个发上来，每个单元打成一个 11 字节数据包。主机要做的就是把这条一维的包序列重新摆回二维矩阵。

**生产者—消费者与有界队列。** 把「读 USB + 解析 + 拼帧」这件耗时且耗时不均的工作放在 GUI 主线程上做，界面会卡顿，USB 缓冲还会溢出丢数据。解法是开一条后台线程专门做这件事，每凑齐一帧就塞进一个**有界队列**；显示线程从队列里取帧来画图。队列满了就丢旧帧——对雷达而言，「最新的画面」永远比「完整的 backlog」重要。这就是「采集/显示解耦」。

术语速查：

| 术语 | 含义 |
|---|---|
| 数据包（data packet） | 11 字节，承载 1 个 range×doppler 单元的 I/Q 与检测位，帧头 `0xAA`、帧尾 `0x55` |
| 状态包（status packet） | 26 字节，回读 FPGA 寄存器，帧头 `0xBB`、帧尾 `0x55` |
| residual（残差） | 一次读取末尾没能凑成完整包的字节，留到下一次读取前面拼接 |
| `_sample_idx` | 帧内一维样本计数器，0..2047，决定样本落在哪个 `[rbin, dbin]` |
| RadarFrame | 一张完整的 64×32 帧，包含 I/Q、幅度、检测、距离剖面等数组 |

## 3. 本讲源码地图

本讲只涉及两个核心源码文件，外加一个重导出胶水文件和一个测试文件用于实践。

| 文件 | 作用 |
|---|---|
| [9_Firmware/9_3_GUI/radar_protocol.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py) | **协议层单一真源**。定义常量、`RadarFrame`、`RadarProtocol`（包解析/构造）、`RadarAcquisition`（采集线程）、`FT2232HConnection`/`FT601Connection`（USB 连接） |
| [9_Firmware/9_3_GUI/v7/workers.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py) | **消费侧 QThread**。`RadarDataWorker` 拥有采集线程与帧队列，把整帧转成 PyQt 信号 |
| [9_Firmware/9_3_GUI/v7/hardware.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/hardware.py) | 重导出胶水：把 `radar_protocol` 里的类重新暴露给 `v7` 包 |
| [9_Firmware/9_3_GUI/test_GUI_V65_Tk.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_GUI_V65_Tk.py) | 协议层单元测试，是本讲实践的依据 |

一个容易踩的坑：`workers.py` 里写的是 `from .hardware import (RadarAcquisition, RadarFrame, ...)`（见 [workers.py:27-33](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L27-L33)），看起来采集线程在 `hardware.py` 里。其实不是——`hardware.py` 只是把它从 `radar_protocol` **重导出**了一遍（[hardware.py:26-35](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/hardware.py#L26-L35)），真正干活的全在 `radar_protocol.py`。读源码时直奔 `radar_protocol.py` 即可。

---

## 4. 核心概念与源码讲解

### 4.1 采集线程：后台线程 + 有界队列的解耦模式

#### 4.1.1 概念说明

「采集线程」回答的问题是：**谁来读 USB、谁来拼帧，又怎样把结果安全地交给 GUI？**

AERIS-10 的答案是两层线程嵌套：

- **生产者** `RadarAcquisition`：一个普通 `threading.Thread`（守护线程），住在 `radar_protocol.py` 里。它死循环地读 USB、切包、拼帧，把整帧塞进队列。它对 PyQt 一无所知，纯 Python，可被测试和无头脚本直接使用。
- **消费者** `RadarDataWorker`：一个 PyQt `QThread`，住在 `workers.py` 里。它**拥有**一个 `RadarAcquisition` 实例和一个有界队列，从队列里取帧，再通过 PyQt 信号（`frameReady` 等）交给 GUI 主线程画图。

为什么要套两层？因为 PyQt 的信号/槽机制必须从 `QThread` 侧发起，而协议解析逻辑想保持「无 GUI 依赖、可单测」。于是让 `QThread` 当外壳、`threading.Thread` 当引擎，两者用一个 `queue.Queue` 解耦。队列就是它们的「握手带」。

#### 4.1.2 核心流程

生产者 `RadarAcquisition.run()` 的主循环：

```text
residual = b""                       # 上一次读取遗留的尾巴
while 未收到 stop:
    chunk = conn.read(4096)          # 拉一批字节（数量不定）
    若 chunk 为空: 睡 10ms, continue
    raw = residual + chunk           # 把上次的尾巴拼到这次前面
    packets = find_packet_boundaries(raw)   # 找出所有完整包
    若有包: residual = raw[最后一个包的结尾:]   # 新尾巴
    否则:    residual = raw 的最后 52 字节     # 防止纯噪声时无限增长
    for 每个包:
        数据包 → parse_data_packet → _ingest_sample   # 写入当前帧
        状态包 → parse_status_packet → status_callback # 回调上报
```

消费者 `RadarDataWorker.run()` 的主循环：

```text
acquisition = RadarAcquisition(conn, frame_queue, ...)
acquisition.start()                  # 启动生产者线程
while running:
    frame = frame_queue.get(timeout=0.1)   # 阻塞等一帧
    emit frameReady(frame)                 # 给主线程画图
    若配了 DSP: targets = _run_host_dsp(frame); emit targetsUpdated
    emit statsUpdated(帧数/检测数/错误数)
```

两个循环通过 `frame_queue`（`queue.Queue(maxsize=4)`）连接。队列容量只有 4，这就是「有界」——一旦显示跟不上，`_finalize_frame` 会主动丢掉最旧的一帧（见 4.3），保证内存不涨爆、画面始终是最新数据。

#### 4.1.3 源码精读

先看消费者如何创建队列并启动生产者。`RadarDataWorker.__init__` 里创建有界队列：

```python
# workers.py:89-90
self._frame_queue: queue.Queue = queue.Queue(maxsize=4)
```

完整链接：[workers.py:89-90](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L89-L90)（中文：声明一个最大容量为 4 的帧队列，是采集线程与显示线程之间的缓冲）。

`run()` 里把队列连同连接、回调一起喂给 `RadarAcquisition`，然后 `start()` 它，自己进入「等帧—发信号」循环：

```python
# workers.py:113-119  创建并启动生产者线程
self._acquisition = RadarAcquisition(
    connection=self._connection,
    frame_queue=self._frame_queue,
    recorder=self._recorder,
    status_callback=self._on_status,
)
self._acquisition.start()
```

消费者取帧与发信号的循环（关键几行）：

```python
# workers.py:122-142  取帧、发 frameReady、跑主机端 DSP、发统计
while self._running:
    frame: RadarFrame = self._frame_queue.get(timeout=0.1)
    self._frame_count += 1
    self.frameReady.emit(frame)
    if self._processor is not None:
        targets = self._run_host_dsp(frame)
        ...
    self.statsUpdated.emit({...})
```

完整链接：[workers.py:122-145](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L122-L145)（中文：以 100ms 超时阻塞取帧；取到就发 `frameReady`，再按需跑聚类/跟踪，最后广播帧/检测/错误计数）。注意 `get(timeout=0.1)` 拿不到帧时抛 `queue.Empty`，被 `except queue.Empty: continue` 默默吞掉（[workers.py:144-145](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L144-L145)），这正是「采集慢于显示时显示线程不报错、只是空转」。

再看生产者。`RadarAcquisition` 继承 `threading.Thread`，构造时以守护线程方式启动，并持有「当前正在拼的帧」和样本计数器：

```python
# radar_protocol.py:712-729
class RadarAcquisition(threading.Thread):
    def __init__(self, connection, frame_queue, recorder=None, status_callback=None):
        super().__init__(daemon=True)
        self.conn = connection
        self.frame_queue = frame_queue
        ...
        self._frame = RadarFrame()      # 当前正在拼装的帧
        self._sample_idx = 0            # 帧内一维样本计数
        self._frame_num = 0
```

完整链接：[radar_protocol.py:712-729](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L712-L729)（中文：`daemon=True` 表示主进程退出时它自动结束，不会卡住关闭流程；`_frame`/`_sample_idx` 是帧组装的状态机记忆）。

生产者的主循环 `run()` 见 [radar_protocol.py:734-776](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L734-L776)（中文：残差拼接 → 边界扫描 → 逐包分发；其中残差拼接的 4.1.2 伪代码对应这里的 `raw = residual + chunk` 与尾部留存逻辑）。状态包分支还会把自测试/AGC 信息打日志，并通过 `status_callback` 回调到 `RadarDataWorker._on_status`（[workers.py:159-161](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L159-L161)），后者再发 `statusReceived` 信号。

#### 4.1.4 代码实践

**实践目标**：用 mock 连接端到端跑通「采集线程 → 队列 → 取到一帧」，亲眼看到生产者—消费者握手。

**操作步骤**（在 `9_Firmware/9_3_GUI/` 目录下执行；以下为示例代码，非项目原有脚本）：

```python
# 示例代码：最小化复现采集→队列→取帧
import queue, time
from radar_protocol import FT2232HConnection, RadarAcquisition

conn = FT2232HConnection(mock=True)   # 无需真实硬件
conn.open()
fq = queue.Queue(maxsize=4)
acq = RadarAcquisition(connection=conn, frame_queue=fq)
acq.start()

frame = fq.get(timeout=5)             # 阻塞等第一帧
print("frame_number =", frame.frame_number)
print("detection_count =", frame.detection_count)
print("range_doppler_i.shape =", frame.range_doppler_i.shape)

acq.stop(); acq.join(timeout=2); conn.close()
```

**需要观察的现象**：

1. `acq.start()` 后程序不会卡死，约零点几秒内 `fq.get()` 就返回一帧（mock 每次读取内部 `sleep(0.05)`，单次 `read(4096)` 约产出 `4096//11≈372` 个包，凑齐 2048 个样本约需 6 次读取）。
2. `range_doppler_i.shape` 应为 `(64, 32)`，即 `NUM_RANGE_BINS × NUM_DOPPLER_BINS`。
3. mock 在 range bin≈20、doppler bin≈8 处注入了一个目标，`detection_count` 应为非零正数（具体值待本地验证）。

**预期结果**：成功打印一个 `frame_number=0`、`(64, 32)` 形状、`detection_count > 0` 的帧，证明采集线程独立把字节流拼成了完整帧并通过队列交出。若你不想手写，可直接跑测试 `test_mock_read_contains_valid_packets`（见 4.2.4）观察同等行为。

#### 4.1.5 小练习与答案

**练习 1**：把 `queue.Queue(maxsize=4)` 改成 `maxsize=1`，显示线程故意 `time.sleep(0.5)` 慢于采集，会发生什么？
**答**：队列很快被填满，`_finalize_frame` 走「丢旧帧」分支（4.3 会讲），新帧不断顶掉旧帧，显示线程永远只拿到最新帧，内存不会涨。这正是有界队列提供的背压（backpressure）。

**练习 2**：为什么 `RadarAcquisition` 用 `threading.Thread` 而 `RadarDataWorker` 用 `QThread`？
**答**：采集与解析逻辑要保持「无 PyQt 依赖、可在测试/无头环境复用」，故用标准库 `threading.Thread`；而要把结果送进 GUI 必须用 PyQt 信号，信号必须从 `QThread` 侧发出，所以外层套 `QThread`。两者用 `queue.Queue` 解耦，各取所需。

---

### 4.2 包边界扫描：残差拼接与帧头帧尾定位

#### 4.2.1 概念说明

`find_packet_boundaries` 回答的问题是：**给定一坨裸字节，哪些字节段是一个完整的数据包/状态包？**

它靠「帧头 + 帧尾」的成对括号来成帧：

- 数据包：以 `0xAA` 开头、隔 10 个字节后必须是 `0x55`（共 11 字节）。
- 状态包：以 `0xBB` 开头、隔 25 个字节后必须是 `0x55`（共 26 字节）。

只看帧头不够——数据载荷里某个样本字节完全可能恰好等于 `0xAA`，造成「假帧头」。所以必须再校验「预期位置上的帧尾」是否也是 `0x55`，双重确认才认定这是一个包。

「残差（residual）」回答的是另一个问题：**一个包被 USB 读取边界切成两半怎么办？** 因为读取边界是任意的，包被腰斩必然发生。解法是：每次处理完所有完整包后，把剩余的尾巴字节存进 `residual`，下一次读取时拼到新 `chunk` 前面再扫描。这样无论包怎么跨越读取边界，都不会丢字节。

#### 4.2.2 核心流程

`find_packet_boundaries(buf)` 的扫描算法（线性扫描 + 跳跃）：

```text
i = 0
while i < len(buf):
    若 buf[i] == 0xAA:
        end = i + 11
        若 end <= len(buf) 且 buf[end-1] == 0x55:   # 帧尾匹配 → 真包
            记录 (i, end, "data"); i = end          # 跳到包末尾继续
        否则若 end > len(buf): break                 # 越界 → 不完整，留给残差
        否则: i += 1                                 # 帧尾不匹配 → 假帧头，跳 1 字节
    若 buf[i] == 0xBB:
        同理用 size=26 判定状态包
    否则: i += 1                                     # 普通字节，跳过
```

注意三种「跳法」的差异：真包跳到 `end`（快）、假帧头跳 1 字节（慢，继续找）、不完整包直接 `break`（把尾巴留给残差）。

`run()` 里的残差维护逻辑（与上面扫描配合）：

```text
若这一轮找到了包: residual = raw[最后一个包结尾:]     # 干净的尾巴
否则:              residual = raw 的最后 52 字节       # 一包都没找到时的防膨胀
```

其中 `52 = 2 × max(11, 26)`，是「无包可解」时残差的上限，防止输入变成纯噪声/纯垃圾时残差无限增长把内存吃光。

#### 4.2.3 源码精读

先看协议常量定义，它们是 FPGA 与主机之间的硬契约：

```python
# radar_protocol.py:38-48
HEADER_BYTE = 0xAA
FOOTER_BYTE = 0x55
STATUS_HEADER_BYTE = 0xBB
DATA_PACKET_SIZE = 11               # 1 + 4 + 2 + 2 + 1 + 1
STATUS_PACKET_SIZE = 26              # 1 + 24 + 1
NUM_RANGE_BINS = 64
NUM_DOPPLER_BINS = 32
NUM_CELLS = NUM_RANGE_BINS * NUM_DOPPLER_BINS  # 2048
```

完整链接：[radar_protocol.py:38-48](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L38-L48)（中文：帧头/帧尾字节、两种包长、以及 64×32=2048 的网格维度，所有扫描与拼帧逻辑都以这些常量为锚）。

`find_packet_boundaries` 的完整实现：

```python
# radar_protocol.py:264-293
def find_packet_boundaries(buf: bytes) -> list[tuple[int, int, str]]:
    packets = []
    i = 0
    while i < len(buf):
        if buf[i] == HEADER_BYTE:
            end = i + DATA_PACKET_SIZE
            if end <= len(buf) and buf[end - 1] == FOOTER_BYTE:
                packets.append((i, end, "data"))
                i = end
            else:
                if end > len(buf):
                    break              # 末尾不完整包 — 留给残差
                i += 1                 # 帧尾不匹配 — 假帧头，跳过
        elif buf[i] == STATUS_HEADER_BYTE:
            end = i + STATUS_PACKET_SIZE
            if end <= len(buf) and buf[end - 1] == FOOTER_BYTE:
                packets.append((i, end, "status"))
                i = end
            else:
                if end > len(buf):
                    break
                i += 1
        else:
            i += 1
    return packets
```

完整链接：[radar_protocol.py:264-293](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L264-L293)（中文：用「头尾双确认」区分真包/假帧头/不完整包；返回 `(起始, 结束, 类型)` 三元组列表，注意它只切边界、不解析内容，解析交给 `parse_*`）。注意它只检查 `buf[end-1]`（帧尾位置）一个字节，是有意的快速结构校验——载荷内出现 `0xAA` 但 10 字节后不是 `0x55` 就会被当假帧头略过。

残差维护在生产者主循环里：

```python
# radar_protocol.py:743-754
raw = residual + chunk
packets = RadarProtocol.find_packet_boundaries(raw)
if packets:
    last_end = packets[-1][1]
    residual = raw[last_end:]                 # 新残差 = 最后一个包之后的部分
else:
    max_residual = 2 * max(DATA_PACKET_SIZE, STATUS_PACKET_SIZE)
    residual = raw[-max_residual:] if len(raw) > max_residual else raw
```

完整链接：[radar_protocol.py:743-754](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L743-L754)（中文：`raw = residual + chunk` 是残差拼接的核心一行；有包时残差取末尾干净段，无包时残差钳到 52 字节防膨胀）。这一行就是「跨读取边界不丢字节」的全部秘密。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「被腰斩的包」靠残差拼接被还原。

**操作步骤**：先跑现成测试确认边界扫描正确，再手写一段把一个包切成两半的演示。

```bash
# 1) 跑协议测试（在 9_Firmware/9_3_GUI/ 下）
python -m pytest test_GUI_V65_Tk.py::TestRadarProtocol -q
```

相关测试：`test_find_boundaries_mixed` 验证「噪声 + 数据包 + 状态包」混杂时能正确数出 3 个包（[test_GUI_V65_Tk.py:247-255](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_GUI_V65_Tk.py#L247-L255)）；`test_find_boundaries_truncated` 验证「只有 6 字节的半包」不会被当成合法包（[test_GUI_V65_Tk.py:260-265](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_GUI_V65_Tk.py#L260-L265)）。

```python
# 示例代码：模拟包被腰斩，观察残差拼接
from radar_protocol import RadarProtocol, HEADER_BYTE, FOOTER_BYTE
import struct

# 造一个完整 11 字节数据包
pkt = bytes([HEADER_BYTE]) + struct.pack(">hhhh", 1, 2, 3, 4) + bytes([0]) + bytes([FOOTER_BYTE])
assert len(pkt) == 11

# 模拟第一次读取只拿到前 6 字节、第二次拿到后 5 字节
chunk1, chunk2 = pkt[:6], pkt[6:]

# 第一次：前 6 字节不构成完整包，应返回空
print("第一次扫描:", RadarProtocol.find_packet_boundaries(chunk1))   # 预期 []

# 残差拼接后扫描：完整包应被还原
raw = chunk1 + chunk2
print("拼接后扫描:", RadarProtocol.find_packet_boundaries(raw))      # 预期 [(0, 11, 'data')]
```

**需要观察的现象**：第一次扫描返回 `[]`（半包被正确忽略，留给残差）；`chunk1 + chunk2` 拼接后扫描返回 `[(0, 11, 'data')]`，证明被腰斩的包在拼接后完整还原。

**预期结果**：输出 `第一次扫描: []` 与 `拼接后扫描: [(0, 11, 'data')]`。这就是 `run()` 里 `raw = residual + chunk` 能救回跨边界包的原理。

#### 4.2.5 小练习与答案

**练习 1**：如果数据载荷里恰好有一个字节是 `0xAA`，而它后面第 10 个字节也恰好是 `0x55`，会发生什么？
**答**：扫描器会把它误判成一个数据包并交给 `parse_data_packet`。这种「假包」概率很低（需 11 字节特定模式同时对齐），且即便发生，解析出的 I/Q 也只是一帧中的一个异常单元，影响有限。这是「头尾双确认」相对「只看帧头」的提升，但并非密码学级别的防伪。

**练习 2**：为什么无包可解时残差要钳到 52 字节，而不是清空？
**答**：因为「无包」可能只是因为这个包还没读完（半包在缓冲末尾）。清空就会丢掉这个半包的前半段，后续永远拼不回。保留最多 52 字节（`2 × max(11,26)`）恰好能容纳一个完整状态包外加余量，既不丢半包，又能在输入真变成纯垃圾时把残差限制在有界范围内。

---

### 4.3 帧组装：样本索引到 range/doppler bin 的映射

#### 4.3.1 概念说明

`_ingest_sample` 回答的问题是：**拿到一个解析好的数据包（一个 range×doppler 单元），该把它写到 64×32 矩阵的哪个格子？**

答案是靠一个一维计数器 `_sample_idx` 推导二维坐标。FPGA 按固定顺序发送 2048 个单元：**doppler 是快变下标、range 是慢变下标**，即先把 range bin 0 的 32 个 doppler 全发完，再发 range bin 1 的 32 个……所以：

\[
\text{sample\_idx} = \text{rbin} \times \text{NUM\_DOPPLER\_BINS} + \text{dbin}
\]

反推坐标：

\[
\text{rbin} = \text{sample\_idx} \, /\!/ \, 32, \qquad
\text{dbin} = \text{sample\_idx} \bmod 32
\]

一个值得点名的命名陷阱：数据包里的 `range_i`/`range_q` 字段并非「填进 Range-Doppler 矩阵的量」，而是**匹配滤波（脉冲压缩）后的距离剖面样本**；真正填进二维矩阵的是 `doppler_i`/`doppler_q`（Doppler FFT 输出）。`range_*` 字段被累加进一维的 `range_profile` 数组。读源码时别被名字带偏。

帧组装的「完成判据」也很简单：`_sample_idx` 自增到 `NUM_CELLS`（2048）就调用 `_finalize_frame()`，把整帧入队、重置状态机，开始下一帧。

#### 4.3.2 核心流程

`_ingest_sample(sample)` 每收到一个包执行：

```text
rbin = _sample_idx // 32      # 0..63
dbin = _sample_idx %  32      # 0..31
若坐标合法:
    frame.range_doppler_i[rbin, dbin] = sample["doppler_i"]   # Doppler 实部
    frame.range_doppler_q[rbin, dbin] = sample["doppler_q"]   # Doppler 虚部
    frame.magnitude[rbin, dbin]   = |doppler_i| + |doppler_q|  # L1 幅度
    若 sample["detection"]:
        frame.detections[rbin, dbin] = 1
        frame.detection_count += 1
    frame.range_profile[rbin] += |range_i| + |range_q|         # 距离剖面累加
_sample_idx += 1
若 _sample_idx >= 2048:
    _finalize_frame()          # 整帧入队 + 重置
```

`_finalize_frame()` 的流程：

```text
给帧打时间戳和帧号
frame_queue.put_nowait(frame)          # 入队
若队列满: 丢最旧帧(get_nowait) 再 put  # 丢旧帧背压
若在录制: recorder.record_frame(frame) # 写 HDF5
_frame_num += 1
_frame = RadarFrame()                  # 新帧
_sample_idx = 0                        # 计数器归零
```

注意一个真实代码里的设计取舍：`parse_data_packet` 其实还解析出了 `frame_start` 位（detection 字节的 bit7，标志一帧的第一个样本），但 `_ingest_sample` **并未使用**它来重新对齐——帧组装完全依赖「严格数满 2048 个样本」这一计数契约。这意味着：一旦中途丢包，整帧之后的所有单元都会错位，直到凑满 2048 才会「自然复位」。`frame_start` 是已解析但未接线的防御性字段。

#### 4.3.3 源码精读

先看 11 字节数据包的字段布局与解析，理解每个样本里有什么：

```python
# radar_protocol.py:177-215
@staticmethod
def parse_data_packet(raw: bytes) -> dict[str, Any] | None:
    # Packet format (11 bytes):
    #   Byte 0:    0xAA (header)
    #   Bytes 1-2: range_q[15:0] MSB first
    #   Bytes 3-4: range_i[15:0] MSB first
    #   Bytes 5-6: doppler_real[15:0] MSB first
    #   Bytes 7-8: doppler_imag[15:0] MSB first
    #   Byte 9:    {7'b0, cfar_detection}
    #   Byte 10:   0x55 (footer)
    ...
    range_q = _to_signed16(struct.unpack_from(">H", raw, 1)[0])
    range_i = _to_signed16(struct.unpack_from(">H", raw, 3)[0])
    doppler_i = _to_signed16(struct.unpack_from(">H", raw, 5)[0])
    doppler_q = _to_signed16(struct.unpack_from(">H", raw, 7)[0])
    det_byte = raw[9]
    detection = det_byte & 0x01
    frame_start = (det_byte >> 7) & 0x01
    return {..., "detection": detection, "frame_start": frame_start}
```

完整链接：[radar_protocol.py:177-215](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L177-L215)（中文：11 字节包逐字段大端解析；`>H` 是大端无符号 16 位，`_to_signed16` 再把 ≥0x8000 的值转成负数以还原有符号 I/Q；detection 在 bit0、frame_start 在 bit7）。`_to_signed16` 的实现见 [radar_protocol.py:156-159](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L156-L159)（中文：标准二进制补码还原 `val - 0x10000 if val >= 0x8000 else val`）。

承接整张帧的 `RadarFrame` 数据类，定义了所有数组形状：

```python
# radar_protocol.py:110-125
@dataclass
class RadarFrame:
    timestamp: float = 0.0
    range_doppler_i: np.ndarray = field(default_factory=lambda: np.zeros((64, 32), dtype=np.int16))
    range_doppler_q: np.ndarray = field(default_factory=lambda: np.zeros((64, 32), dtype=np.int16))
    magnitude:       np.ndarray = field(default_factory=lambda: np.zeros((64, 32), dtype=np.float64))
    detections:      np.ndarray = field(default_factory=lambda: np.zeros((64, 32), dtype=np.uint8))
    range_profile:   np.ndarray = field(default_factory=lambda: np.zeros(64, dtype=np.float64))
    detection_count: int = 0
    frame_number:    int = 0
```

完整链接：[radar_protocol.py:110-125](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L110-L125)（中文：一张帧包含 64×32 的 I/Q/幅度/检测四个矩阵、64 长的一维距离剖面、检测计数与帧号；注意 I/Q 是 `int16`，幅度是 `float64`）。

样本到二维坐标的映射与写入，本讲的核心：

```python
# radar_protocol.py:778-801
def _ingest_sample(self, sample: dict):
    rbin = self._sample_idx // NUM_DOPPLER_BINS     # // 32  → 0..63
    dbin = self._sample_idx % NUM_DOPPLER_BINS      # % 32  → 0..31
    if rbin < NUM_RANGE_BINS and dbin < NUM_DOPPLER_BINS:
        self._frame.range_doppler_i[rbin, dbin] = sample["doppler_i"]
        self._frame.range_doppler_q[rbin, dbin] = sample["doppler_q"]
        mag = abs(int(sample["doppler_i"])) + abs(int(sample["doppler_q"]))
        self._frame.magnitude[rbin, dbin] = mag
        if sample.get("detection", 0):
            self._frame.detections[rbin, dbin] = 1
            self._frame.detection_count += 1
        ri = int(sample.get("range_i", 0)); rq = int(sample.get("range_q", 0))
        self._frame.range_profile[rbin] += abs(ri) + abs(rq)   # 距离剖面跨 doppler 累加
    self._sample_idx += 1
    if self._sample_idx >= NUM_CELLS:
        self._finalize_frame()
```

完整链接：[radar_protocol.py:778-801](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L778-L801)（中文：用 `//32`、`%32` 把一维计数映射到 `[rbin, dbin]`；矩阵填 Doppler 值、距离剖面累加 range 值；注意 `range_profile[rbin] +=` 是跨同一 range bin 的 32 个 doppler 累加，所以一帧结束后 `range_profile` 是该距离门上所有速度的总能量）。

凑齐 2048 后的入队与重置：

```python
# radar_protocol.py:803-823
def _finalize_frame(self):
    self._frame.timestamp = time.time()
    self._frame.frame_number = self._frame_num
    try:
        self.frame_queue.put_nowait(self._frame)        # 入队
    except queue.Full:                                  # 队列满 → 丢旧帧背压
        with contextlib.suppress(queue.Empty):
            self.frame_queue.get_nowait()
        self.frame_queue.put_nowait(self._frame)
    if self.recorder and self.recorder.recording:
        self.recorder.record_frame(self._frame)
    self._frame_num += 1
    self._frame = RadarFrame()                          # 重置：新帧
    self._sample_idx = 0                                # 计数器归零
```

完整链接：[radar_protocol.py:803-823](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L803-L823)（中文：打时间戳→入队→队列满时丢最旧帧再入队→按需录 HDF5→帧号自增→新建空帧、计数器归零，完成状态机一轮循环）。

#### 4.3.4 代码实践

**实践目标**：追踪一个 11 字节数据包从 `read()` 到 `range_doppler_i[rbin, dbin]` 的全过程，并验证 `_sample_idx` 到 2048 触发入队。

**操作步骤**（示例代码）：

```python
# 示例代码：单步追踪一个样本的落点 + 触发整帧入队
import queue
from radar_protocol import RadarProtocol, RadarAcquisition, NUM_DOPPLER_BINS, NUM_CELLS

# 1) 直接调 _ingest_sample，观察坐标映射
acq = RadarAcquisition(connection=None, frame_queue=queue.Queue(maxsize=4))
for idx, expected in [(0,(0,0)), (1,(0,1)), (31,(0,31)), (32,(1,0)), (2047,(63,31))]:
    acq._sample_idx = idx
    rbin = idx // NUM_DOPPLER_BINS
    dbin = idx %  NUM_DOPPLER_BINS
    print(f"idx={idx:>4} -> (rbin,dbin)=({rbin},{dbin})  预期{expected}")

# 2) 喂一个样本，看它真的写进矩阵
acq._sample_idx = 32 * 20 + 8          # range bin 20, doppler bin 8
acq._ingest_sample({"doppler_i": 1234, "doppler_q": -500,
                    "detection": 1, "range_i": 0, "range_q": 0})
print("range_doppler_i[20,8] =", acq._frame.range_doppler_i[20,8])   # 预期 1234
print("detections[20,8]      =", acq._frame.detections[20,8])        # 预期 1
print("detection_count       =", acq._frame.detection_count)         # 预期 1

# 3) 让 _sample_idx 达到 NUM_CELLS，观察整帧入队
acq._sample_idx = NUM_CELLS            # 2048
acq._ingest_sample({"doppler_i":0,"doppler_q":0,"detection":0,"range_i":0,"range_q":0})
print("队列中帧数 =", acq.frame_queue.qsize())                       # 预期 1
print("_sample_idx 重置为 =", acq._sample_idx)                       # 预期 0
```

**需要观察的现象**：

1. 五个 `idx` 的 `(rbin,dbin)` 映射全部符合预期，证明 doppler 快变、range 慢变。
2. 第 2 步 `range_doppler_i[20,8]` 变成 1234、检测位与计数同步更新，证明样本确实按计算坐标写入了对应格子。
3. 第 3 步一旦 `_sample_idx` 触及 2048，队列立刻多出 1 帧、`_sample_idx` 归零，证明 `_finalize_frame` 被触发并完成了状态机重置。

**预期结果**：坐标映射、矩阵写入、整帧入队三项均与上面注释一致。注意第 3 步里 `_ingest_sample` 在 `>= NUM_CELLS` 时其实先把当前样本写进「越界坐标」会被 `if rbin < NUM_RANGE_BINS ...` 拦下（idx=2048 时 rbin=64 越界），所以那次写入无效，但随后的 `_finalize_frame` 仍会照常触发——这正是计数契约的边界行为（具体写不写进越界格待本地验证，但入队触发是确定的）。

#### 4.3.5 小练习与答案

**练习 1**：为什么矩阵填的是 `doppler_i/doppler_q`，而 `range_profile` 填的是 `range_i/range_q`？
**答**：因为这张 64×32 矩阵本身就是 **Range-Doppler 图**——两个轴分别是距离与速度，格子里的值是 Doppler FFT 的输出，所以用 `doppler_*`。而 `range_i/range_q` 是更上游的匹配滤波（脉冲压缩）输出，只按距离组织、没有速度维，所以累加进一维的 `range_profile`，用来画「距离剖面」曲线。字段名反映的是信号处理阶段，不是「最终画在哪个图」。

**练习 2**：若 FPGA 偶发丢了一个数据包，这套纯计数组装会出什么问题？`frame_start` 位能救吗？
**答**：丢一个包后，后续 2047 个单元都会错位一格（range/doppler 全部错位），直到本帧凑满 2048 才自然复位，下一帧重新从 0 开始——所以影响被限制在「最多坏一帧」。当前 `_ingest_sample` 并没有用 `frame_start` 位做重新对齐（它被解析但未接线），所以**现在**救不了；但这个位的存在正是为「未来加一层 `if sample['frame_start']: 重置 _sample_idx` 的再同步逻辑」预留的接口。

---

## 5. 综合实践

把本讲三块知识串起来：**端到端追踪一个 11 字节数据包，并解释整帧入队的触发点。**

请按下面的提示，写一段「追踪日志」，把一个数据包的一生讲清楚：

1. **起点 `read()`**：在 [radar_protocol.py:738](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L738) 处 `chunk = self.conn.read(4096)` 拿到一批字节，其中包含我们关心的那个包（可能被切成两半）。
2. **残差拼接**：在 [radar_protocol.py:743](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L743) 处 `raw = residual + chunk`，把它和上次尾巴拼成连续字节流。
3. **边界扫描**：`find_packet_boundaries(raw)`（[radar_protocol.py:744](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L744)）靠 `0xAA`+`0x55` 头尾双确认，把这个包的 `(start, end, "data")` 报出来。
4. **解析**：`parse_data_packet(raw[start:end])`（[radar_protocol.py:757-758](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L757-L758)）把 11 字节拆成 `doppler_i/doppler_q/detection/...` 字典。
5. **写入矩阵**：`_ingest_sample(parsed)`（[radar_protocol.py:760](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L760)）用 `_sample_idx//32`、`%32` 算出 `(rbin, dbin)`，把 `doppler_i` 写进 `self._frame.range_doppler_i[rbin, dbin]`（[radar_protocol.py:784](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L784)）。
6. **触发入队**：`_sample_idx` 自增到 2048 时（[radar_protocol.py:800-801](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L800-L801)）调用 `_finalize_frame`，把整帧 `put_nowait` 进 `frame_queue`（[radar_protocol.py:812](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L812)）。
7. **跨线程交付**：`RadarDataWorker.run()` 在另一条线程 `frame_queue.get()`（[workers.py:125](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L125)）拿到这帧，发 `frameReady` 信号给 GUI 主线程画图。

**交付物**：一张包含上述 7 步的流程图（手画或文字版皆可），并在第 6 步旁注明「`_sample_idx` 达到 `NUM_CELLS=2048` 是唯一的入队触发条件，与 `frame_start` 位无关」。

**进阶（可选）**：用 4.1.4 的 mock 代码实际跑出第一帧，在 `_ingest_sample` 里临时加一行 `print(self._sample_idx, rbin, dbin)`（修改的是你自己的调试副本，不要提交），观察前 35 个样本的坐标序列是否确实是 `(0,0),(0,1),...,(0,31),(1,0),...`。修改局部参数观察行为属于源码阅读型实践，记得改完还原。

## 6. 本讲小结

- 采集用了**两层线程**：`RadarAcquisition`（`threading.Thread`，纯 Python 引擎）负责读 USB/拼帧，`RadarDataWorker`（`QThread`）负责取帧发 PyQt 信号，两者用 `queue.Queue(maxsize=4)` 解耦。
- USB 是字节流不是消息流，**残差拼接**（`raw = residual + chunk`）是跨读取边界不丢字节的关键；无包时残差钳到 52 字节防膨胀。
- **`find_packet_boundaries`** 用「帧头 `0xAA`/`0xBB` + 帧尾 `0x55`」双确认成帧，区分真包、假帧头、不完整包三种情况。
- 11 字节数据包逐字段大端解析：`range_q/range_i`（匹配滤波输出）+ `doppler_i/doppler_q`（Doppler FFT 输出）+ detection（bit0）/frame_start（bit7）。
- **`_ingest_sample`** 用 `_sample_idx //32`、`%32` 把一维计数映射到 `[rbin, dbin]`，Doppler 值进二维矩阵、range 值累加进一维距离剖面。
- 整帧入队**唯一**由 `_sample_idx >= NUM_CELLS(2048)` 触发；队列满时丢最旧帧做背压；`frame_start` 位当前解析但未用于再同步。

## 7. 下一步学习建议

- **承接显示与目标形成**：本讲产出的 `RadarFrame` 接下来会被 `RadarDataWorker._run_host_dsp` 消费，做 DBSCAN 聚类与 Kalman 跟踪。那是下一篇 **u8-l3（信号处理、聚类与目标跟踪）** 的主题，建议接着读 `v7/processing.py` 与 `v7/models.py`。
- **回看协议全貌**：本讲聚焦「数据包→帧」，状态包的 6 个 32 位字位布局在 u6-l2 已讲过；若想复习 opcode↔Verilog case 的硬契约，可重读 `radar_protocol.py` 的 `Opcode` 枚举（[radar_protocol.py:53-103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L53-L103)）。
- **录制与回放**：`_finalize_frame` 里调用的 `DataRecorder.record_frame` 把帧写进 HDF5（[radar_protocol.py:678-693](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L678-L693)）；回放侧的 `ReplayWorker`（[workers.py:411-573](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/workers.py#L411-L573)）发同样的信号，让 live 与 replay 对显示层无差别——值得作为「同一接口两种实现」的延伸阅读。
- **动手验证**：把 4.1.4 / 4.2.4 / 4.3.4 三个实践都跑一遍，再用 `test_GUI_V65_Tk.py::TestRadarProtocol` 全套测试做交叉验证，能最大化巩固本讲。
