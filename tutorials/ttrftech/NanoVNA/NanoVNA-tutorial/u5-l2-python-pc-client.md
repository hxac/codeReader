# Python 上位机：串口协议与数据分析

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `python/nanovna.py` 如何把固件的 USB CDC Shell 命令封装成 Python API，理解 `getport()` 按 VID/PID 自动发现设备的原理。
2. 掌握上位机与固件之间的「文本协议」：回显、`\r\n` 行结尾、`ch> ` 提示符如何充当数据帧的结束哨兵。
3. 读懂 `data` / `scan` / `frequencies` / `capture` 四条数据通道的封装与解包格式，包括 `capture` 二进制流中 RGB565 字节序的三次交换巧合。
4. 能独立编写 `plot_s11.py` 这样的自动测量与绘图脚本，并在 Jupyter 中用 scikit-rf 做交互分析和 Touchstone 存档。
5. 学会「批判性阅读」：识别 `nanovna.py` 中与当前固件不匹配的历史遗留代码路径。

## 2. 前置知识

本讲建立在 u5-l1（USB CDC Shell）之上，回顾几个关键结论：

- **CDC 虚拟串口**：固件枚举为一个 VID=0x0483（ST）/ PID=0x5740 的 USB CDC-ACM 设备，PC 端看到一个普通串口，逻辑与 USB 细节解耦。
- **回显（echo）**：`VNAShell_readLine` 每从流里读到一个可打印字符就原样写回（人用终端时能看到自己敲的字）；收到 `\r` 时输出 `\r\n` 并开始执行命令。
- **提示符（prompt）**：每条命令执行完后，shell 主循环打印提示符 `ch> `（`main.c:44` 的 `VNA_SHELL_PROMPT_STR`），然后等待下一行。对脚本来说，提示符就是「命令的响应已经发完」的信号。
- **CMD_WAIT_MUTEX**：`data`、`scan`、`capture` 等命令带此标志，main 线程只登记函数指针 `shell_function`，由 sweep 线程在测量间隙执行——所以上位机不需要任何锁，协议天然串行（详见 u5-l1）。
- **measured 数组**：测量结果是复数 S11/S21，存于 `measured[2][101][2]`（通道 × 频点 × 实虚部），频点数上限 `POINTS_COUNT = 101`（[nanovna.h:40](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L40)）。
- **校准槽位复用**（u3-l2）：`cal_data[5]` 在 `cal_done` 之前存标准件原始测量，之后存解出的误差项 Ed/Es/Er/Et/Ex——这直接决定 `data 2`～`data 6` 读到的是什么。

不熟悉的术语：

- **帧定界 / 哨兵（sentinel）**：串口是字节流，没有「消息边界」。上位机需要一个特殊标记来判断「响应到此为止」，这里就是提示符 `ch>`。
- **字节序**：一个 16 位数的两个字节谁先谁后。STM32 是小端（低字节在低地址、先发送），`struct.unpack(">H")` 中的 `>` 表示按大端（第一个字节当高位）解释。
- **RGB565**：用 16 位表示一个像素：红 5 位、绿 6 位、蓝 5 位。
- **Touchstone（.s1p/.s2p）**：射频领域通用的 S 参数文本文件格式，scikit-rf 可以读写。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/nanovna.py](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py) | 上位机核心库：`NanoVNA` 类 + 命令行入口，本讲主角 |
| [python/NanoVNA-example.ipynb](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/NanoVNA-example.ipynb) | Jupyter 交互示例：抓数据、画图、Smith 圆图、Touchstone 存档 |
| [python/README.md](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/README.md) | 用法速查：安装依赖与常用命令行 |
| [python/requirements.txt](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/requirements.txt) | 依赖清单：matplotlib / scikit-rf / pillow / pyserial |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | 固件侧协议实现：`cmd_data` / `cmd_scan` / `cmd_frequencies` / `cmd_capture`、命令表、shell 收发循环 |
| [ili9341.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c) | `ili9341_read_memory`：capture 命令的数据源（读显存） |
| [chprintf.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c) | 裁剪版 printf：`%f` 的精度决定 `data` 文本格式 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | `POINTS_COUNT`、`RGB565` 宏、ETERM 槽位编号 |

## 4. 核心概念与源码讲解

### 4.1 NanoVNA 类封装：设备发现与命令通道

#### 4.1.1 概念说明

上位机脚本的本质是「把人敲键盘的动作自动化」：人面对终端敲 `data 0` 回车、肉眼看数字；脚本面对同一个虚拟串口写 `data 0\r`、用代码读回字节并解析。`NanoVNA` 类把这整套交互收拢成三件事：

1. **找到设备**——不用用户手填 `/dev/ttyUSB0` 或 `COM3`，按 USB 的 VID/PID 对自动扫描；
2. **管理连接**——懒打开（第一次用的时候才 `open`）、可重复 `close`；
3. **统一收发**——`send_command` 负责「写命令 + 丢掉回显」，是所有高层方法的地基。

#### 4.1.2 核心流程

一次 `send_command("data 0\r")` 在时间轴上是这样的：

```text
PC 端                          固件端 (main 线程 shell)
  |                               |
  | 逐字节写出 "data 0\r"          | VNAShell_readLine 每读 1 字节回显 1 字节
  |                               |
  |<── 回显 "data 0" ────────────|  收到 '\r' 时打印 "\r\n"，开始解析
  |                               |  命令带 CMD_WAIT_MUTEX：
  |                               |    shell_function = cmd_data
  |                               |    每 100ms 轮询，sweep 线程执行完才返回
  |<── "0.123 -0.456\r\n" ×101 ──|  cmd_data 在 sweep 线程里逐行打印
  |<── "ch> " ───────────────────|  shell 主循环打印提示符，等待下一行
```

关键点：`send_command` 里的 `self.serial.readline()` 把第一行（回显 + `\r\n`）整体丢掉，之后的字节流才是纯数据。这就是注释 `# discard empty line` 的实际作用——丢弃的不是「空行」而是「回显行」。

#### 4.1.3 源码精读

**设备发现：按 VID/PID 扫描串口列表。**

[python/nanovna.py:L8-L17](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L8-L17) 定义了 ST 的厂商 ID 和产品 ID，`getport()` 遍历操作系统枚举出的所有串口，找到 vid/pid 匹配的那个就返回其设备节点名，找不到则抛 `OSError("device not found")`。

这两个常量与固件 USB 描述符严格对应——[usbcfg.c:L38-L39](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/usbcfg.c#L38-L39) 的设备描述符里写着 `idVendor = 0x0483`、`idProduct = 0x5740`。这就是「免配置发现」的全部秘密：插上任何一块刷了此固件的 NanoVNA，脚本都能自己找到它。

**类的骨架：连接懒打开 + 频点表缓存。**

[python/nanovna.py:L21-L35](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L21-L35) 是构造函数与频点管理：`dev` 参数允许用户显式指定串口（绕过自动发现），否则调 `getport()`；`points = 101` 与固件的 `POINTS_COUNT` 一致；`set_frequencies` 用 `np.linspace` 在 PC 侧生成**标称**频点表（注意是浮点近似，后面 4.2 会讲为什么精确频点应以设备为准）。

[python/nanovna.py:L37-L48](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L37-L48) 是 `open`/`close`/`send_command`。`open` 是幂等的（已打开就不重开），`send_command` 的三步：确保打开 → 写命令（注意结尾的 `\r`，固件只认 `\r` 为回车）→ `readline()` 丢回显。

**固件侧的另一半：回显与提示符从哪来。**

[main.c:L2231-L2265](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2231-L2265) 的 `VNAShell_readLine`：L2260 的 `streamPut(shell_stream, c)` 就是回显；L2250-L2253 收到 `\r` 时打印 `VNA_SHELL_NEWLINE_STR`（即 `\r\n`）并返回。退格键处理（L2241-L2247）是给人用的，脚本不会触发。

[main.c:L2443-L2448](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2443-L2448) 是 shell 主循环：每轮先打印提示符 `ch> `（`VNA_SHELL_PROMPT_STR`，[main.c:L44](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L44)），再读一行、执行一行。提示符永远在命令响应之后出现——这是下一节 `fetch_data` 帧定界的依据。

**一堆「弱类型」setter。**

[python/nanovna.py:L51-L75](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L51-L75) 用 `if xxx is not None` 的模式把 `sweep`/`freq`/`port`/`gain`/`offset`/`power` 等命令各包一层，参数为 `None` 就跳过。这是「命令模式」的 Python 化：每条 shell 命令对应一个同名或近名方法。

#### 4.1.4 代码实践

**实践目标**：亲眼看到操作系统枚举出的串口带着 vid/pid 属性，理解自动发现的依据；无硬件时也能运行并观察输出。

**操作步骤**（示例代码，PC 上运行）：

```python
# list_ports_check.py —— 观察 pyserial 看到的串口及 VID/PID
from serial.tools import list_ports

for p in list_ports.comports():
    print(f"device={p.device!r} vid={p.vid:#06x} pid={p.pid:#06x} "
          f"manufacturer={p.manufacturer!r}")
print("scan done")
```

运行：`python3 list_ports_check.py`（需要 `pip3 install pyserial`）。

**需要观察的现象**：

- 有 NanoVNA 接入时，列表中应出现一行 `vid=0x0483 pid=0x5740`，其 `device` 字段就是 `getport()` 会返回的串口名；
- 不接设备时脚本只打印其他串口（或什么都没有）和 `scan done`，不报错——`getport()` 在这种情况下才会抛 `OSError`。

**预期结果**：接设备时能定位到 `vid=0x0483, pid=0x5740` 的那一行（待本地验证：取决于本机是否有硬件及其他串口设备）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `send_command` 结尾必须是 `\r` 而不能是 `\n`？

**答案**：`VNAShell_readLine` 只把 `\r`（码 13）当作回车（[main.c:L2250](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2250)）；`\n`（码 10）小于 0x20，落入 L2256-L2257 的「其他控制字符直接跳过」分支，既不回显也不结束行，命令永远不会被执行。

**练习 2**：如果去掉 `send_command` 里的 `readline()`，后续 `fetch_data` 读到的第一行会是什么？

**答案**：是回显行——你发送的命令文本本身加上 `\r\n`（如 `"data 0\r\n"`）。以 `data 0` 为例，解析时会把 `"data"` 和 `"0"` 当成两个浮点数去 `float()`，直接抛 `ValueError`。`readline()` 恰好读到第一个 `\n` 为止，把回显整行吞掉。

**练习 3**：`getport()` 在两台 NanoVNA 同时插入时会怎样？

**答案**：它返回**第一台**匹配的设备（遍历顺序取决于操作系统枚举顺序），另一台被忽略。想指定设备应走 `NanoVNA("/dev/ttyUSB1")` 这种显式 `dev` 参数路径（[python/nanovna.py:L22-L23](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L22-L23)）。

### 4.2 文本数据抓取与解包：frequencies / data / scan

#### 4.2.1 概念说明

这是上位机最核心的一层：把「设备的内存数组」搬到「PC 的 numpy 数组」。NanoVNA 提供三条互补的文本通道：

- `frequencies`：只读当前频点表（整数，每行一个）；
- `data {0-6}`：只读某个数组当前内容（复数，每行「实部 虚部」），设备保持连续扫描；
- `scan {start} {stop} [points]`：**一次性**改频点表 → 暂停 → 完整扫一遍 →（可选）直接连数据一起输出，专为脚本取数设计。

三者共享同一个读回机制 `fetch_data`，其关键问题是：**串口是字节流，怎么知道响应结束了？** 答案是拿提示符 `ch>` 当哨兵。

#### 4.2.2 核心流程

`fetch_data` 的定界算法（伪代码）：

```text
line = ""
loop:
    c = 读一个字符
    line += c                      # 注意：'\r' 也会被加进去
    if c == '\n':
        result += line; line = ""  # 一行完结，积进 result
    if line 以 "ch>" 结尾:
        break                      # 提示符出现 → 响应结束
return result                      # 只含数据行，不含提示符
```

`data(0)` 的完整取数流程：

```text
send_command("data 0\r")     → 写命令，readline() 吞掉回显 "data 0\r\n"
fetch_data()                 → 读 101 行 "re im\r\n"，遇 "ch>" 停
逐行 split(' ') → float(d[0]) + float(d[1])*1j
np.array(...)                → 长度 101 的复数数组，即 S11
```

`scan()` 的分段取数流程：

```text
若没有频点表 → fetch_frequencies() 从设备读
while 还有频点没扫:
    取接下来 101 个频点的首尾 → send_scan(首, 尾, 个数)
    data(0) 追加进 array0      # CH0 反射
    data(1) 追加进 array1      # CH1 传输
resume()                      # 恢复设备的连续扫描状态
```

#### 4.2.3 源码精读

**帧定界 `fetch_data`。**

[python/nanovna.py:L80-L95](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L80-L95)：逐字符读、按 `\n` 切行、以 `line.endswith('ch>')` 作为停止条件。这段 15 行的代码藏着三个值得玩味的细节：

1. **L86 和 L91 的 `next` 是无效语句**。`next` 是 Python 内建函数名，单独成行只是一条「求值后丢弃」的表达式语句——作者显然想写 `continue`。后果是 `\r` 字符仍然进了 `line`（「ignore CR」的注释并没有生效）。幸运的是下游解析用 `line.strip()` 或 `float()` 的空白容忍把 `\r` 消化了，所以无碍。这是阅读开源代码时常见的「注释意图 ≠ 代码行为」案例。
2. **提示符是 `ch> `（带尾随空格），为什么 `endswith('ch>')` 能停住？** 因为判断发生在**每收到一个字符之后**：`'c'`、`'h'`、`'>'` 依次到达，收到 `'>'` 的那一刻 `line == "ch>"` 成立、立即 break——尾随的空格还没到。要是固件把提示符改成 `c h>` 或 `ch:>`，这里就会失配死等。
3. **break 之后，那个尾随空格还留在串口缓冲区里**。它会被下一次 `send_command` 的 `readline()` 一并吞进「回显行」里丢弃，所以协议整体自洽。

**`data` 的文本格式与固件 `%f` 精度。**

[python/nanovna.py:L162-L170](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L162-L170)：发送 `data {sel}`，把每行按空格切成两个 float 组成复数。

固件侧 [main.c:L682-L701](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L682-L701) 的 `cmd_data` 决定了数组语义：

- `sel = 0 / 1` → `measured[sel]`，即 CH0（S11 反射）与 CH1（S21 传输）；
- `sel = 2..6` → `cal_data[sel-2]`，按 [nanovna.h:L62-L66](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L62-L66) 的编号依次是 Ed（直接性）、Es（源匹配）、Er（反射跟踪）、Et（传输跟踪）、Ex（隔离）——但注意 u3-l2 讲过的槽位复用：没做 `cal_done` 时这些槽里存的是标准件原始测量值。

每行用 `shell_printf("%f %f\r\n", ...)` 输出。`%f` 的精度由裁剪版 printf 决定：[chprintf.c:L393](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L393) 在未指定精度时取 `FLOAT_PRECISION = 9`（[chprintf.c:L43](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L43)），且 [chprintf.c:L40](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chprintf.c#L40) 定义了 `CHPRINTF_FORCE_TRAILING_ZEROS`，即固件输出形如 `-0.042419024` 的固定位数小数——比 float 本身的有效位数还多，Python 端 `float()` 解析毫无压力。

**`frequencies` 与频点表的「真相源」。**

[python/nanovna.py:L172-L179](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L172-L179) 发送 `frequencies` 命令，把每行一个整数读成 `np.array` 并**缓存回 `self._frequencies`**。

固件侧 [main.c:L1808-L1817](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1808-L1817) 的 `cmd_frequencies` 遍历 `frequencies[]` 打印，`if (frequencies[i] != 0)` 跳过 0——这不是防御式编程，而是 u3-l1 讲过的设计：频点表尾部清零兼作 sweep 循环的哨兵，`sweep_points` 之后的 0 本来就不该输出。

一个容易踩的坑：`set_frequencies` 用 `np.linspace` 生成的是**浮点近似**，而固件的 `set_frequencies` 用整数误差扩散生成整数频点，两者可能有 ±1Hz 级差异。要做严谨的频率轴，应当用 `fetch_frequencies()` 从设备读回真值——`scan()` 方法开头（L191-L192）正是这么做的。

**`scan` 命令：为脚本取数量身定制的路径。**

[python/nanovna.py:L181-L185](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L181-L185) 的 `send_scan` 只发三个参数（start/stop/points）。固件侧 [main.c:L899-L940](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L899-L940) 的 `cmd_scan` 做了四件事，每一件都对应前几讲的结论：

- L923 `set_frequencies(start, stop, points)`：**临时借表**——只改 `frequencies[]` 数组，不动 `frequency0/frequency1` 持久配置（u3-l1 的「绕过正门」）；
- L924-L925：若开了自动插值且校准在应用态，先 `cal_interpolate` 把误差项搬到新频点上（u3-l3）；
- L926-L927：`pause_sweep()` 后调 `sweep(false)`——`break_on_operation = false` 意味着这趟扫描**不被 UI 打断**，保证 101 点数据完整一致（u2-l5）；
- L929-L938：**可选的第 4 参数 outmask**——bit0 输出频率、bit1 输出 CH0 数据、bit2 输出 CH1 数据，一行一个频点地连数据一起吐，省去再发 `data` 命令的往返（源码注释 `faster data recive`）。`nanovna.py` 没有用这个特性，而是分段后各发一次 `data`。

`cmd_scan` 结束时**不恢复扫描**——设备停在暂停态。所以 Python 侧 [python/nanovna.py:L187-L204](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L187-L204) 的 `scan()` 在循环结束后必须调 `resume()`。固件的 `cmd_resume`（[main.c:L297-L308](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L297-L308)）不止是置回 `SWEEP_ENABLE`：它先 `update_frequencies()` 用持久配置**重建频点表**（把 `scan` 借走的表还回去），再按需补一次校准插值。暂停/恢复的一对底层函数见 [main.c:L151-L161](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L151-L161)，就是对 `sweep_mode` 的 `SWEEP_ENABLE` 位做清零/置位。

**为什么连续扫描时读 `data` 不会读到「半新半旧」的数据？**

看命令表 [main.c:L2165](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2165)：`data` 带 `CMD_WAIT_MUTEX`。main 线程把它登记进 `shell_function` 后轮询等待（[main.c:L2299-L2304](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2299-L2304)），sweep 线程在自己的循环里、**两次测量之间**消费它（[main.c:L120-L126](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L120-L126)）。也就是说 `cmd_data` 逐行打印时，`sweep()` 并没有并发地往 `measured[]` 里写——线程安全不靠锁，靠「挪到同一个线程里执行」。这对上位机是透明的：它只觉得 `data` 命令「有点慢」。

**历史遗留路径：`gamma` 命令已被固件禁用。**

[python/nanovna.py:L127-L133](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L127-L133) 的 `fetch_gamma` 发送 `gamma` 命令读单点反射系数，`scan_gamma`（L158-L160）逐频点调用它。但当前固件里 [main.c:L747-L762](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L747-L762) 的 `cmd_gamma` 整段被 `#if 0` 包住，命令表里对应行也被注释（[main.c:L2176](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2176)）——发 `gamma` 只会得到 `gamma?` 的「未知命令」回应。同类问题：`reflect_coeff_from_rawwave`（L135-L146）用了 `signal.hilbert`、`fetch_rawwave`（L106-L116）用了 `time.sleep`，而文件头部（L2-L6）既没 `import scipy.signal` 也没 `import time`，真调用会抛 `NameError`。**结论：现代用法走 `scan()`/`data()`，这些旧方法是仓库演化留下的化石**——阅读上位机代码时必须对照固件命令表（[main.c:L2153-L2208](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153-L2208)）逐一核实命令是否真的存在。

顺带一提，`data 0` 取到的值**是否经过校准修正**取决于设备端 `CALSTAT_APPLY` 状态（u3-l3）：sweep 每个频点测完就地做 `apply_error_term_at` 原地覆盖 `measured[]`，所以上位机拿到的永远是「当前生效口径」的数据——校准开着就是修正后的，关着就是原始值。

#### 4.2.4 代码实践

**实践目标**：在不碰硬件的情况下，把 `data` 命令的文本解包逻辑在 PC 上跑通，验证自己对协议格式的理解。

**操作步骤**（示例代码，PC 上运行，无需设备）：

```python
# parse_data_replay.py —— 用一段伪造的 "data 0" 响应走通解包链
# 伪造内容模仿 fetch_data() 的真实返回：每行 "re im\r\n"，无提示符
fake_response = (
    "0.099203465 0.024510924\r\n"
    "0.098812445 0.023118972\r\n"
    "-0.301125526 -0.042419024\r\n"
)

# 与 nanovna.py data() 相同的解析逻辑（L162-L170）
x = []
for line in fake_response.split('\n'):
    if line:
        d = line.strip().split(' ')
        x.append(float(d[0]) + float(d[1]) * 1.j)
print([complex(v) for v in x])
```

运行：`python3 parse_data_replay.py`。

**需要观察的现象**：三行文本变成三个复数；行尾的 `\r` 被 `strip()` 消化；空行（`split('\n')` 的最后一个空串）被 `if line:` 过滤。

**预期结果**：输出 `[(0.0992+0.0245j), (0.0988+0.0231j), (-0.3011-0.0424j)]`（复数打印格式因 Python 版本略有差异）。有真机的读者可把 `fake_response` 换成 `nv.data(0)` 前用 `fetch_data()` 抓到的真实文本对比（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`data 3` 读到的是什么？什么情况下它有意义？

**答案**：`sel=3` 落在 `2..6` 区间，读 `cal_data[3-2] = cal_data[1]`，即 Es 槽位。它只在完成 `cal_done` 之后才是「源匹配误差项」；在那之前这个槽里存的是 OPEN 标准件的原始测量（u3-l2 的两阶段复用）。

**练习 2**：为什么 `scan()` 每段只扫 101 个点，而不是一次 `scan 1e6 900e6 801`？

**答案**：固件把点数硬限制在 `POINTS_COUNT = 101`（[main.c:L917-L920](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L917-L920) 会拒绝超限值并报 `sweep points exceeds range`），因为 `measured[]`/`frequencies[]` 都是编译期按 101 定维的数组。要更密的点只能像 `scan()` 那样分段测量再在 PC 端拼接。

**练习 3**：如果调用 `scan()` 后忘记 `resume()`，设备会怎样？怎么恢复？

**答案**：设备停在暂停态（屏幕上扫描不再刷新，`sweep_mode` 的 `SWEEP_ENABLE` 位为 0）。手动在终端发一条 `resume` 即可；`cmd_resume` 会顺带用持久配置重建频点表并恢复校准插值，所以之前 `scan` 临时借用的频点表也会被还原。

### 4.3 二进制抓取：capture 屏幕截图与字节序

#### 4.3.1 概念说明

`capture` 是协议里唯一的**二进制**通道：让固件把 LCD 显存整屏读回并原样吐出，PC 端重组成一张 PNG。它与文本通道的规则不同——没有行结构、没有提示符定界，长度固定为 \(320 \times 240 \times 2 = 153600\) 字节，PC 端按字节数硬读。

这条通道最有趣的地方是**字节序**：数据在到达 PC 之前经历了两次「交换」，恰好互相抵消。理解它等于把 u4-l1（RGB565 宏）、u5-l1（streamPut）和本讲的 struct 解包串成一条线。

#### 4.3.2 核心流程

```text
PC: send_command("capture\r")          # readline() 吞掉回显 "capture\r\n"
固件: 循环 120 次, 每次读 2 行显存:
        ili9341_read_memory(0, y, 320, 2, 640, spi_buffer)
        逐字节 streamPut 共 4*320=640 字节       # RGB565, 低字节在前
PC: serial.read(320*240*2)             # 恰好 153600 字节
    struct.unpack(">76800H", b)        # 大端解包成 76800 个 16 位
    掩码运算转 RGBA → PIL Image → img.save("out.png")
```

字节序的三级接力（核心）：

1. **固件宏做第一次交换**。ili9341 读回的是每像素 3 字节的 RGB888，固件用自定义宏重打包成 16 位。对照标准形式

   \[ S_{\text{std}} = (r_{7..3} \ll 11) \,\vert\, (g_{7..2} \ll 5) \,\vert\, b_{7..3} \]

   而 [nanovna.h:L304](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L304) 的宏把绿位拆到两端，逐位展开可验证它恰好等于 \( \mathrm{swap}(S_{\text{std}}) \)（高 8 位与低 8 位互换）——这是 u4-l1 讲过的、与 SPI 先发低字节配套的排布。
2. **小端内存发送保持这个交换**。`streamPut` 按地址递增吐字节，小端机器上低地址是低字节，所以线上顺序是 `[W_lo, W_hi]`，其中 \( W = \mathrm{swap}(S_{\text{std}}) \)，即线上是 `[S_hi, S_lo]`。
3. **Python 大端解包做第二次交换**。`">H"` 把先到的字节当高位，于是 \( V = (W_{lo} \ll 8) \,\vert\, W_{hi} = \mathrm{swap}(W) = S_{\text{std}} \)。两次交换抵消，Python 手里已经是**标准 RGB565**——所以 L213 的三个掩码（`0xF800` 取红、`0x07E0` 取绿、`0x001F` 取蓝）能直接按教科书位段切颜色。

#### 4.3.3 源码精读

**PC 端 `capture`。**

[python/nanovna.py:L206-L214](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L206-L214)：发命令 → 硬读 153600 字节 → 大端解包 76800 个半字 → 位运算组装成 32 位 RGBA：

\[ \text{RGBA} = \underbrace{0xFF000000}_{\alpha} + \underbrace{(V \wedge 0xF800) \gg 8}_{R} + \underbrace{(V \wedge 0x07E0) \ll 5}_{G} + \underbrace{(V \wedge 0x001F) \ll 19}_{B} \]

在小端机器上，32 位整数的最低字节对应 `'raw','RGBA'` 模式的 R 通道，于是红绿蓝 alpha 各就各位，`Image.frombuffer` 零拷贝成图。注意 `0x07E0` 是 6 位掩码、结果乘上 `<<5` 后绿通道占据高 6 位——正对应 RGB565 里绿有 6 位、红蓝各 5 位。

**固件端 `cmd_capture`：复用 spi_buffer 分批搬运。**

[main.c:L727-L745](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L727-L745)：16KB SRAM 放不下整屏帧缓冲（u4-l1 的总前提），所以每次只读 **2 行**（640 像素）进 `spi_buffer`，再逐字节 `streamPut` 发走；240 行共 120 批。L733-L734 的编译期断言 `SPI_BUFFER_SIZE < (3*320+1)` 保证读缓冲装得下一批——读显存时每像素要暂存 3 字节 RGB888。这也解释了为什么命令带 `CMD_WAIT_MUTEX`（[main.c:L2190](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2190)）：它独占 `spi_buffer`，绝不能和 sweep 线程的绘图任务并发（u5-l3 会展开这个复用的风险面）。

**数据源 `ili9341_read_memory`。**

[ili9341.c:L475-L517](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L475-L517)：设好列/页地址窗口后发 `ILI9341_MEMORY_READ`，清空 SPI 接收 FIFO、跳过 1 个 dummy 字节，然后双 DMA（收通道 + 只发 dummy 字节产生时钟的发通道）把 `len*3+1` 字节收进 `spi_buffer`；L505-L516 再把每 3 字节一组解出 R/G/B，用 [nanovna.h:L304](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L304) 的 RGB565 宏压成 16 位**原地**写回——注意读入缓冲和输出缓冲是同一块内存，解析指针始终走在 DMA 写入的数据之后，这是嵌入式里常见的「就地重排」。

**命令行捷径。**

[python/README.md:L24-L26](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/README.md#L24-L26) 给出 `./nanovna.py -C out.png`；对应 CLI 代码 [python/nanovna.py:L382-L386](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L382-L386)。

#### 4.3.4 代码实践

**实践目标**：用两个像素走通「固件宏 → 小端发送 → 大端解包 → RGBA」全链路，验证字节序推导（纯 PC 端，无需硬件）。

**操作步骤**（示例代码，PC 上运行）：

```python
# capture_byteorder_check.py —— 两个像素验证 capture 字节序链
import struct

def fw_rgb565(r, g, b):   # 逐位照抄 nanovna.h:304 的宏
    return (((g & 0x1c) << 11) | ((b & 0xf8) << 5) |
            (r & 0xf8) | ((g & 0xe0) >> 5)) & 0xFFFF

pixels = [(255, 0, 0), (0, 0, 255)]        # 纯红、纯蓝
words = [fw_rgb565(*p) for p in pixels]

# 模拟 cmd_capture 的线上字节流: 小端内存按地址递增发送 → 低字节在前
wire = b"".join(bytes([w & 0xFF, w >> 8]) for w in words)

# 模拟 nanovna.py capture() 的解包与转换 (L210-L213)
arr = struct.unpack(">2H", wire)
for v in arr:
    rgba = (0xFF000000 + ((v & 0xF800) >> 8) +
            ((v & 0x07E0) << 5) + ((v & 0x001F) << 19))
    print(f"unpacked={v:#06x}  R={rgba & 0xFF} G={(rgba >> 8) & 0xFF} "
          f"B={(rgba >> 16) & 0xFF} A={rgba >> 24}")
```

运行：`python3 capture_byteorder_check.py`。

**需要观察的现象**：纯红像素解包后 R 分量非零、G=B=0；纯蓝像素 B 分量非零、R=G=0；alpha 恒为 255。

**预期结果**（由源码逐位推导，待本地运行验证）：纯红 → `unpacked=0xf800, R=248, G=0, B=0, A=255`；纯蓝 → `unpacked=0x001f, R=0, G=0, B=248, A=255`。若把 `">2H"` 改成 `"<2H"`（小端解包），两个颜色会互换位段、输出错乱——这本身就是「两次交换抵消」的反证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `capture` 之后 PC 端不担心剩下的 `ch> ` 提示符污染下一次读取？

**答案**：`capture()` 只读固定 153600 字节，提示符在其后到达、留在串口缓冲区；下一次 `send_command` 的 `readline()` 会把它连同新命令的回显一起吞掉。二进制通道的定界靠长度，提示符只是「留给文本通道的尾巴」。

**练习 2**：固件为什么每批只读 2 行而不是 4 行或 1 行？

**答案**：约束来自 `spi_buffer` 的大小。读显存时每像素需暂存 3 字节 RGB888，一批 n 行需要 \( n \times 320 \times 3 + 1 \) 字节（dummy 字节），2 行 = 1921 字节，而 `SPI_BUFFER_SIZE` 是 2048——2 行是安全上限内的最大整行数；L733-L734 的 `#error` 断言就是在编译期守护这个不等式。

**练习 3**：如果想在 PC 端把截图存成无 alpha 的 RGB 格式，最少改哪里？

**答案**：把 [python/nanovna.py:L213](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L213) 的 `0xFF000000 +` 去掉（alpha 变 0），并把 `Image.frombuffer` 的模式从 `'RGBA'` 改成 `'RGB'`、掩码组装改成按 R/G/B 各占一字节的排布即可；更省事的做法是拿现有 RGBA 图直接 `img.convert('RGB')`。

### 4.4 Jupyter 分析流程：绘图、TDR 与 Touchstone 存档

#### 4.4.1 概念说明

拿到复数数组只是起点，射频工程师的日常是「看图」：对数幅度（dB）、相位、驻波比、Smith 圆图、时域（TDR）。`nanovna.py` 把这些常用后处理做成了**以 `self.frequencies` 为横轴的绘图方法**，配合 Jupyter 的 `%matplotlib inline` 形成交互式工作流；再借助 scikit-rf（skrf）接入更大的射频工具生态——Smith 圆图绘制、Touchstone 文件读写。

这一层的价值在于：**固件屏幕只有 320×240 和 4 条轨迹，PC 上你可以任意叠加、缩放、存档、复算**。

#### 4.4.2 核心流程

notebook 的标准流水线（对应 [python/NanoVNA-example.ipynb](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/NanoVNA-example.ipynb) 各 cell）：

```text
%matplotlib inline
from nanovna import NanoVNA
nv = NanoVNA()                      # 自动发现并连接
nv.set_sweep(1e6, 300e6)            # 发 sweep start/stop 命令
nv.fetch_frequencies()              # 从设备读回整数频点表（横轴真值）
s11 = nv.data(0); s12 = nv.data(1)  # 拉取两通道复数数据
nv.logmag(s11); nv.phase(s12)       # 内置绘图
n = nv.smith(s11)                   # 经 skrf 画 Smith 圆图, 返回 Network
n.write_touchstone('xxx-s11')       # 存 .s1p, 可被任何射频软件读取
```

常用显示格式与复数的换算（与固件 plot.c 的 12 种格式同源，u4-l2）：

\[ |S11|_{\mathrm{dB}} = 20 \log_{10} |\Gamma|, \qquad \mathrm{VSWR} = \frac{1+|\Gamma|}{1-|\Gamma|} \]

#### 4.4.3 源码精读

**绘图助手一族。**

[python/nanovna.py:L216-L234](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L216-L234)：`logmag` 画 \(20\log_{10}|\cdot|\)、`phase` 用 `np.angle` 并可选 `np.unwrap`（展开 ±180° 跳变，否则相频图上会看到锯齿）。[python/nanovna.py:L248-L252](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L248-L252) 的 `vswr` 直接套用上面的公式（u2-l1 练习里手算过）。它们都先用 `pl.xlim(self.frequencies[0], self.frequencies[-1])` 锁定横轴——这就是为什么用这些方法前要先 `set_sweep` + `fetch_frequencies` 把频点表备好。

**TDR：PC 版时域变换。**

[python/nanovna.py:L260-L270](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L260-L270)：对频域 Γ 加 Blackman 窗、补零到 256 点做 `np.fft.ifft`，时间轴总长取 \(1/(f_1 - f_0)\)（相邻频点间隔的倒数，即测量总带宽决定的最大无模糊时窗）。这与固件 `transform_domain`（u3-l5）思路完全一致——加窗压旁瓣、零填充提高采样密度——只是固件用 Kaiser 窗且多了 `wincorr` 幅度补偿，PC 版更简化。对比两者输出是理解时域变换的好练习。

**接入 scikit-rf。**

[python/nanovna.py:L280-L290](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L280-L290)：`skrf_network` 把「频点数组 + S 参数数组」包装成 skrf 的 `Network` 对象（注意频率要除以 1e6 换成 MHz——skrf 的 `from_f` 默认单位是 MHz，单位弄错会差 6 个数量级）；`smith` 调 `n.plot_s_smith()` 画圆图。notebook 后半段演示 `write_touchstone('100mhz-lpf-s11')` 导出 `.s1p`、再 `skrf.Network(...)` 读回，以及 skrf 私有 `.ntwk` 格式的读写闭环。

**CLI 入口：同一套 API 的另一种壳。**

[python/nanovna.py:L316-L438](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L316-L438) 用 optparse 把类方法映射成命令行开关：`-p` 对应 `logmag`、`-s` 对应 `smith`、`-C` 对应 `capture`、`-e` 直接发裸命令、`-o` 导 Touchstone。L402-L415 的取数策略值得注意：默认（点数 ≤101 且未指定 `-c`）走「设备连续扫描 + `set_sweep` + `data`」的轻量路径；点数 >101 或指定 `-c` 才走分段 `scan()`——因为后者会暂停设备、逐段重扫，代价更高。[python/README.md:L12-L30](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/README.md#L12-L30) 列了最常用的几条命令。

#### 4.4.4 代码实践

**实践目标**：体验「numpy 复数数组 → 多种物理量视图」的纯计算部分，不依赖硬件与串口。

**操作步骤**（示例代码，PC 上运行，需 `pip3 install numpy matplotlib`）：

```python
# views_practice.py —— 对同一段 S11 数据换三种"镜头"
import numpy as np
import matplotlib.pyplot as plt

# 模拟一个 50MHz 低通滤波器的 S11: 通带内反射小, 阻带内接近全反射
freq = np.linspace(1e6, 300e6, 101)
fc = 50e6
gamma = 0.95 / (1 + 1j * freq / fc)          #随手构造的演示模型

fig, ax = plt.subplots(3, 1, figsize=(7, 8), sharex=True)
ax[0].plot(freq / 1e6, 20 * np.log10(np.abs(gamma))); ax[0].set_ylabel("|S11| (dB)")
ax[1].plot(freq / 1e6, np.rad2deg(np.angle(gamma)));  ax[1].set_ylabel("phase (deg)")
ax[2].plot(freq / 1e6, (1 + np.abs(gamma)) / (1 - np.abs(gamma)))
ax[2].set_ylabel("VSWR"); ax[2].set_xlabel("frequency (MHz)")
for a in ax: a.grid(True)
plt.tight_layout(); plt.show()
```

运行：`python3 views_practice.py`。

**需要观察的现象**：三幅图共享横轴；同一复数数组在 dB/相位/VSWR 三个「镜头」下呈现完全不同的形状；`np.angle` 默认输出 ±180° 包裹的相位。

**预期结果**：|S11| 从约 -0.4dB 平滑下降到深谷后又回升；相位连续变化但被包裹在 ±180° 内；VSWR 在通带内接近 1、阻带内很大（构造的是演示模型，非真实器件曲线）。

#### 4.4.5 小练习与答案

**练习 1**：`nv.logmag(s11)` 之前忘了 `fetch_frequencies()` 会发生什么？

**答案**：`self.frequencies` 可能是 `set_frequencies` 用 linspace 生成的浮点近似（横轴与实际测量频点有微小偏差）；若从未设置过则属性为 `None`，`self.frequencies[0]` 抛 `TypeError`。正确顺序是 `set_sweep` → `fetch_frequencies` → `data` → 绘图。

**练习 2**：`skrf_network` 里为什么频率要 `self.frequencies / 1e6`？

**答案**：`sk.Frequency.from_f(..., unit='mhz')` 声明输入数值的单位是 MHz，而 `self.frequencies` 存的是 Hz，除以 1e6 换算。漏掉这一步，skrf 会把 1MHz 当成 1GHz，所有频率相关计算（如电长度）全错。

**练习 3**：CLI 的 `-e` 选项（[python/nanovna.py:L370-L380](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/nanovna.py#L370-L380)）适合什么场景？

**答案**：快速试命令——它把任意字符串原样发给 shell，不解析响应。适合调试新命令（比如 u5-l1 综合实践新加的 `uptime`）、或者发 `pause`/`resume`/`save 0` 这类不需要读回数据的控制命令；要取数据则必须用会调 `fetch_data` 的封装方法。

## 5. 综合实践

**任务**：编写 `plot_s11.py`——设置 1MHz~900MHz 扫描，抓取 CH0（S11）数据，用 matplotlib 把 \(|S11|\)(dB) 与相位画在同一窗口的两个子图上，并自动标注反射最小点（谐振点）的频率与数值。没有真机的读者用模拟数据走通同一条解包与绘图流水线。

**设计要点**（先想清楚再动手）：

1. 取数路径选哪条？≤101 点且允许设备连续扫描 → `set_sweep` + `fetch_frequencies` + `data(0)` 最轻；要严格「一次性完整快照」 → `scan()`（它内部会 `resume`）。本任务用前者即可。
2. marker 就是 numpy 的一行代码：`i = np.argmin(np.abs(s11))`，再用 `freq[i]` 取频率。
3. 两条曲线共享横轴（`sharex=True`），相位可先不 unwrap，观察包裹现象后再加。

**参考实现**（示例代码）：

```python
#!/usr/bin/env python3
# plot_s11.py —— 抓取/模拟 S11, 双子图绘制并标注最小反射点
import sys
import numpy as np
import matplotlib.pyplot as plt

def fake_s11(freq):
    """无硬件路径: 模拟带通特性 + 噪声, 返回与 nv.data(0) 同构的复数数组"""
    rng = np.random.default_rng(42)
    fc, bw = 100e6, 20e6                          # 中心 100MHz, 带宽 20MHz
    g = 0.9 * (freq - fc) ** 2 / ((freq - fc) ** 2 + (bw / 2) ** 2)
    return g * np.exp(1j * 2 * np.pi * freq * 1e-9) + 0.01 * (rng.standard_normal(len(freq)) + 1j * rng.standard_normal(len(freq)))

use_hw = "--hw" in sys.argv
if use_hw:
    from nanovna import NanoVNA
    nv = NanoVNA()                                # 按 VID/PID 自动发现
    nv.set_sweep(int(1e6), int(900e6))            # sweep start/stop 命令
    nv.fetch_frequencies()                        # 读回整数频点表
    s11 = nv.data(0)                              # CH0, 101 点复数
    freq = nv.frequencies
else:
    freq = np.linspace(1e6, 900e6, 101)
    s11 = fake_s11(freq)

i = int(np.argmin(np.abs(s11)))                   # 反射最小点 = "marker"

fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(8, 7))
ax1.plot(freq / 1e6, 20 * np.log10(np.abs(s11)), lw=1)
ax1.plot(freq[i] / 1e6, 20 * np.log10(abs(s11[i])), "rv")
ax1.annotate(f"marker {freq[i]/1e6:.1f} MHz\n{20*np.log10(abs(s11[i])):.2f} dB",
             xy=(freq[i] / 1e6, 20 * np.log10(abs(s11[i]))),
             xytext=(10, 20), textcoords="offset points",
             arrowprops=dict(arrowstyle="->"))
ax1.set_ylabel("|S11| (dB)"); ax1.grid(True)

ax2.plot(freq / 1e6, np.rad2deg(np.angle(s11)), lw=1)
ax2.plot(freq[i] / 1e6, np.rad2deg(np.angle(s11[i])), "rv")
ax2.set_ylabel("phase (deg)"); ax2.set_xlabel("frequency (MHz)"); ax2.grid(True)

plt.tight_layout()
plt.savefig("plot_s11.png", dpi=120)
print(f"marker: f={freq[i]:.0f} Hz |S11|={20*np.log10(abs(s11[i])):.3f} dB "
      f"phase={np.rad2deg(np.angle(s11[i])):.1f} deg, saved to plot_s11.png")
```

**操作步骤**：

1. 无硬件：`python3 plot_s11.py`（只依赖 numpy/matplotlib）；
2. 有硬件：把 `python/` 目录加入 `PYTHONPATH` 或在同目录运行 `python3 plot_s11.py --hw`（依赖见 [python/requirements.txt](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/python/requirements.txt)）；
3. 进阶：把相位子图改成 `np.unwrap` 版本对比；把 `data(0)` 换成 `scan()[0]` 对比两种取数路径的行为差异（`scan` 会暂停再恢复设备）。

**需要观察的现象**：

- 模拟路径：红色 marker 落在曲线谷底，两幅图的 marker 频率一致；
- 硬件路径：端口开路时 |S11| 接近 0dB；接上天线或滤波器后谷底位置随器件移动。

**预期结果**：生成 `plot_s11.png`，终端打印 marker 的频率/幅度/相位三元组。硬件路径的具体数值取决于被测件与本机校准状态（待本地验证）。

## 6. 本讲小结

- `NanoVNA` 类 = 「自动发现（VID 0x0483/PID 0x5740）+ 懒连接 + `send_command`（写命令、`\r` 结尾、`readline` 吞回显）」，所有高层方法都建立在这条地基上。
- 文本协议的帧定界靠提示符：`fetch_data` 逐字符累积、以 `ch>` 结尾即停；尾随空格「来不及到达」的时序细节和 `next` 无效语句（本想写 `continue`）是这段 15 行代码最值得咀嚼的地方。
- 三条取数通道分工明确：`frequencies` 读整数频点表（尾部 0 哨兵被跳过）、`data {0-6}` 读 measured 或 cal_data 槽位、`scan` 临时借频点表 + 暂停 + `sweep(false)` 一次性完整测量（还支持 outmask 连带输出，`nanovna.py` 未使用）。
- `scan` 之后设备停在暂停态，必须 `resume`；`cmd_resume` 会顺带重建频点表并恢复校准插值。`data`/`scan`/`capture` 都是 `CMD_WAIT_MUTEX` 命令，在 sweep 线程的测量间隙执行，上位机天然读到一致性快照。
- `capture` 是唯一二进制通道：153600 字节 RGB565；固件宏的字节交换 × 小端发送 × Python 大端解包 = 两次交换抵消，PC 端拿到的恰是标准 RGB565。
- 批判性阅读：`fetch_gamma`/`scan_gamma` 依赖的 `gamma` 命令在固件中已被 `#if 0` 禁用，`reflect_coeff_from_rawwave` 还引用了未导入的 `signal`/`time` 模块——读上位机代码必须对照固件命令表逐一核实。

## 7. 下一步学习建议

- **u5-l3（RTOS 资源约束与固件优化）**：本讲看到 `cmd_capture` 分批复用 `spi_buffer` 的手法，下一讲系统分析这类缓冲区复用在 CPU 与 DMA 竞争下的风险，以及如何用 `.su` 栈使用报告做体积分析。
- **u5-l4（二次开发实战）**：毕业项目「测量平均」需要一个 PC 端验证工具——把本讲的 `plot_s11.py` 改造成「开/关平均各抓一次、双曲线叠加对比」的脚本，正是那个项目的验收环节。
- **延伸阅读**：对照固件侧 [main.c:L2153-L2208](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153-L2208) 的完整命令表，给 `nanovna.py` 补上尚未封装的命令（如 `bandwidth`、`marker`、`transform`）是巩固本讲的最好练习；社区后续衍生上位机（如 NanoVNA-Saver）也在这份 `nanovna.py` 的协议思路上发展。
