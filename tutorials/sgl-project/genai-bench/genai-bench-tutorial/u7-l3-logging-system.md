# 日志系统

## 1. 本讲目标

u7-l1 讲清了 worker 日志「怎么跨进程传回 master」，但把日志系统真正的心脏留到了本讲：那些 handler 到底是什么、为什么 master 进程里要「延迟 flush」日志、以及 worker 进程如何重写日志配置才不会污染终端。

本讲聚焦 `genai_bench/logging.py` 这一个文件（并衔接 `distributed/runner.py` 的调用点），围绕三个最小模块展开。学完后你应该能够：

- 说清 **`RollingRichPanelHandler` / `DelayedRichHandler` / `WorkerRichHandler`** 三个 `RichHandler` 子类各自的职责与区别。
- 解释 **为什么在 `rich.live.Live` 上下文里不能直接打印日志，而要先把日志缓冲起来、等 Live 退出再 flush**——这是本讲的核心问题。
- 画出 **worker 日志的「队列回传」在 `logging.py` 侧的实现**：`WorkerLoggingManager` 如何用 `force=True` 重写配置、`WorkerRichHandler` 往队列里塞的是什么。

本讲与 u7-l1（worker 日志的传输路径）和 u7-l2（`Live` 仪表盘与 `with dashboard.live:`）紧密衔接，是它们的「底层注脚」。

## 2. 前置知识

### 2.1 Python logging 的基本三件套

Python 标准库的 `logging` 围绕三个角色运作：

- **Logger（记录器）**：业务代码调用的入口，如 `logger.info("...")`。日志先到 logger。
- **Handler（处理器）**：决定日志「去哪儿」。一条日志可以被多个 handler 各自处理一次。常见的有 `StreamHandler`（打印到终端）、`FileHandler`（写文件）。
- **Formatter（格式化器）**：决定日志「长什么样」，挂在 handler 上。

一条 `logger.info("hello")` 会流经 logger →（它挂载的所有 handler）→ 每个 handler 用自己的 formatter 渲染后输出。`logging.basicConfig(handlers=[...])` 就是给「根 logger」一次性挂上若干 handler。本讲的 `LoggingManager` / `WorkerLoggingManager` 都是对 `basicConfig` 的封装。

### 2.2 rich 与 Live 仪表盘的「抢占式」输出

`rich` 是一个终端美化库。`RichHandler` 是 rich 提供的 logging handler，能把日志渲染成带颜色、带时间戳、带 traceback 高亮的样子。

关键难点在于 **`rich.live.Live`**（见 u7-l2）。当 `Live(..., screen=True)` 启动后，它会 **接管整个终端屏幕**，以固定帧率（本项目 `refresh_per_second=2`）不断重绘一整屏内容。在 Live 运行期间，任何「直接往终端 print / 写 stdout」的输出，都会在 **下一次 Live 重绘时被整屏覆盖掉**——肉眼看到的就是日志一闪而过、或者把仪表盘刷花。

这就是为什么普通日志（走 stdout 的 handler）和 Live 仪表盘 **天然冲突**。`logging.py` 里两个最有意思的 handler——`RollingRichPanelHandler` 和 `DelayedRichHandler`——正是为了解决这个冲突而设计的。

### 2.3 fork 出来的子进程会「继承」父进程的日志配置

`multiprocessing.Process` 在 Linux 上默认用 **fork** 启动子进程：子进程拷贝父进程的全部内存，**包括已经配置好的 logging handler**。也就是说，worker 进程一出生，身上就带着 master 配置的那些 handler（其中有引用 master 的 `layout`、`live` 对象的）。这些 handler 在 worker 进程里既无意义、又可能出错。所以 worker 必须 **重新配置日志**——这正是 `WorkerLoggingManager` 用 `force=True` 的原因（见 4.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [genai_bench/logging.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py) | 本讲主角。定义三个 `RichHandler` 子类、`LoggingManager`（master/单进程）、`WorkerLoggingManager`（worker 进程）、`init_logger` 与 `warning_once`。 |
| [genai_bench/distributed/runner.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py) | 调用点：创建 `worker_log_queue`、worker 进程里实例化 `WorkerLoggingManager`、master 起 `_consume_worker_logs` greenlet 消费队列。 |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | 调用点：构造 `LoggingManager("benchmark", dashboard.layout, dashboard.live, ...)`，并用 `with dashboard.live:` 包住实验循环，循环结束后调 `flush_buffer()`。 |
| [genai_bench/ui/dashboard.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py) | `RichLiveDashboard.live` 就是一个 `Live(layout, screen=True)`，是「延迟 flush」要服务的对象。 |

## 4. 核心概念与源码讲解

### 4.1 rich 日志 handler 家族与 LoggingManager

#### 4.1.1 概念说明

`logging.py` 一共定义了三个 `RichHandler` 的子类，它们长得像、却各司其职，是理解整个日志系统的钥匙：

| Handler | 用在哪 | 做什么 |
|---------|--------|--------|
| `RollingRichPanelHandler` | master 进程（benchmark + UI 开） | 把日志渲染进仪表盘的「Logs」面板，只保留最近 N 条，**不直接碰终端**。 |
| `DelayedRichHandler` | master 进程（benchmark + UI 开） | **缓冲**日志，等 Live 退出后再一次性打印到终端；遇 ERROR 立即 flush 并退出。 |
| `WorkerRichHandler` | worker 进程 | 把日志 **转发给 master**（塞进队列），自己 **既不打印、也不写面板**。 |

而把这三个 handler 组装起来、决定「benchmark 命令挂哪些、excel/plot 命令挂哪些」的，是 `LoggingManager`。它读两个环境变量做分支：

- `GENAI_BENCH_LOGGING_LEVEL`：日志级别，默认 `INFO`，非法值回退 `INFO`。
- `ENABLE_UI`：是否开实时仪表盘，默认开。

无论什么命令，`LoggingManager` 都会先挂一个 **文件 handler**（写 `genai_bench.log`），保证日志一定落盘。差别只在「终端那一路」用什么 handler。

#### 4.1.2 核心流程

`LoggingManager.init_logging` 的分支逻辑：

```
读 log_level、ENABLE_UI
│
├── 永远先建 file_handler（写 genai_bench.log）
│
├── command_type == "benchmark" 且 ENABLE_UI:
│      extra_handlers = init_ui_logging()
│        → RollingRichPanelHandler（写 Logs 面板）
│        → DelayedRichHandler（缓冲，延后打印）   ← 记到 self.delayed_handler
│
├── command_type == "benchmark" 但 ENABLE_UI=false:
│      extra_handlers = [get_console_handler()]（普通 stdout handler）
│
└── 其它命令（excel/plot）:
       extra_handlers = [get_rich_handler()]（普通 RichHandler，直接打印）

logging.basicConfig(level, handlers=[file_handler, *extra_handlers])
设置 sys.excepthook（捕获未处理异常 → 记 ERROR → flush_buffer → 退出）
```

注意一个设计细节：`DelayedRichHandler` 实例被存在 `self.delayed_handler` 上（[logging.py:221](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L221)），但 **并不是所有命令都会有它**——只有 benchmark + UI 开时才创建。所以 CLI 拿到 `delayed_handler` 后还要判空再 `flush_buffer`（见 [cli.py:540-541](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L540-L541)）。`setup_exception_handler` 里同样要判空（[logging.py:145-146](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L145-L146)、[logging.py:155-156](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L155-L156)），就是这个原因。

#### 4.1.3 源码精读

**`RollingRichPanelHandler`——把日志塞进面板、绕开终端**（[logging.py:15-42](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L15-L42)）：

它的诀窍在于构造一个 **写到内存 `StringIO` 的 Console**，而不是写到真正的终端：

```python
self.log_buffer = StringIO()
kwargs["console"] = Console(file=self.log_buffer, width=150)
```

这样 `super().emit(record)`（即 RichHandler 的渲染）只会把渲染好的文本写进 `self.log_buffer` 这个内存字符串，**完全不会碰终端**，自然也就不会和 Live 抢屏幕。然后 `emit` 读取 buffer、只截取最后 `max_entries`（默认 10）行，更新到仪表盘布局的 `logs` 面板上：

```python
log_contents = self.log_buffer.getvalue()
log_lines = log_contents.splitlines()
last_10_lines = log_lines[-self.max_entries:]
trimmed_log_contents = "\n".join(last_10_lines)
self.layout[self.panel_name].update(
    Panel(trimmed_log_contents, title="Logs", border_style="red")
)
```

注意这里 **不刷新 buffer**：每次 `emit` 都读全部历史再截尾。因为日志量在单次实验里不会无限增长（且只保留最后 10 行显示），这种「全读再截」的写法足够简单可靠。面板里的日志是 **随着 Live 的 2Hz 重绘** 自动出现在屏幕上的——因为它写的是 `layout` 对象，而 `layout` 正是 Live 正在重绘的内容。

**`LoggingManager.get_file_handler`——日志永远落盘**（[logging.py:162-181](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L162-L181)）：用普通 `logging.FileHandler`，带级别、时间、模块名格式。`log_dir` 存在就写到该目录下的 `genai_bench.log`，否则写到当前工作目录。

**`init_ui_logging`——benchmark + UI 的双 handler 装配**（[logging.py:205-229](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L205-L229)）：同时返回 `panel_handler` 和 `delayed_handler`。于是 benchmark 期间一条日志会同时被三处处理：文件（留底）、Logs 面板（实时可见）、延迟缓冲（Live 退出一并打印到终端）。

> 小贴士：`logging.py` 还有一个辅助函数 `warning_once`（[logging.py:297-309](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L297-L309)），用进程级集合去重「预期内但吵」的警告（如服务端不返回 usage 时的 token 估算告警，见 u3-l2）。它和本讲三个 handler 不在一层，但同样服务于「别让日志刷屏」这个主题。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `RollingRichPanelHandler` 把日志写进内存 buffer、而不是终端。

**操作步骤**（写一个临时脚本 `tmp_panel_demo.py`，**示例代码，非项目原有文件**）：

```python
# 示例代码
import logging
from io import StringIO
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from genai_bench.logging import RollingRichPanelHandler

layout = Layout()
layout.split_column(Layout(name="logs"))   # 造一个含 "logs" 子区的布局

h = RollingRichPanelHandler(layout=layout)
logger = logging.getLogger("demo")
logger.setLevel(logging.INFO)
logger.addHandler(h)

for i in range(15):                          # 故意超过 max_entries=10
    logger.info("line %d", i)

# 观察 1：终端应当没有任何 "line x" 输出（都进了 buffer）
# 观察 2：layout["logs"] 里只剩最后 10 条
print("--- 终端只会有这一行分隔，上面没有 line x ---")
```

**需要观察的现象**：

1. 运行时，循环里的 15 条 `logger.info` **不会** 出现在终端。
2. `layout["logs"]` 被更新的 Panel 内容只含 `line 5` ~ `line 14` 共 10 条（截掉了前 5 条）。

**预期结果**：证明 `RollingRichPanelHandler` 的输出全部进了 `StringIO`、再转进 layout，终端干净。**待本地验证**：若你的环境未装项目依赖，先 `pip install -e .`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RollingRichPanelHandler` 要把 `Console` 的 `file` 设成一个 `StringIO`，而不是默认的终端？

**参考答案**：因为 benchmark 期间终端被 `rich.Live`（`screen=True`）接管，任何直接写终端的输出都会被 Live 的下一次重绘覆盖掉。把 Console 指向内存 `StringIO`，handler 的渲染结果就不会落终端、也不会和 Live 抢屏；真正要展示给用户的部分，由 handler 显式地写进 `layout["logs"]` 面板，随 Live 重绘自然出现。

**练习 2**：`LoggingManager` 在哪两种情况下 **不会** 创建 `DelayedRichHandler`？

**参考答案**：① 命令是 benchmark 但 `ENABLE_UI=false`（此时用普通 `get_console_handler()`，直接打 stdout）；② 命令不是 benchmark（excel/plot，用普通 `get_rich_handler()`）。只有 benchmark 且 UI 开时，才有 Live 上下文，才需要延迟 flush，才创建 `DelayedRichHandler`。

---

### 4.2 延迟 flush：为什么 Live 上下文里不能直接打印日志

#### 4.2.1 概念说明

这是本讲要回答的核心问题：**为什么在 `with dashboard.live:` 上下文里，日志不能直接打印，而要先缓冲、等 Live 退出再 flush？**

答案分两层：

1. **会被覆盖**：如 2.2 所述，`Live(screen=True)` 全屏重绘，直接 print 的日志一闪即逝。
2. **会刷花仪表盘**：普通 handler 往 stdout 写时，光标位置、清屏状态都由 Live 掌管，外来写入会把仪表盘刷成乱码。

所以 `DelayedRichHandler` 的策略是：**Live 运行期间，把每条日志的 `LogRecord` 对象先攒在内存里（`record_buffer`），一个都不真打印**；等 Live 退出（benchmark 结束、或出错）后，再 `flush_buffer()` 一次性把它们按顺序吐到终端。这样用户在实验结束后，仍能在终端的滚动历史里看到完整的日志。

那 Live 运行期间用户怎么看日志？看 **Logs 面板**——那是 `RollingRichPanelHandler` 的职责（4.1）。两个 handler 互补：面板管「实时预览」，延迟 handler 管「事后全量留存」。

还有一个关键设计：**遇到 ERROR 级别日志，立刻 flush 并退出**。因为 ERROR 通常意味着实验已经没法继续，此时应该先停掉 Live（让终端恢复正常）、把缓冲的日志全打出来方便排错、再 `sys.exit(1)`。

#### 4.2.2 核心流程

`DelayedRichHandler` 的状态机：

```
初始: flush_later = True（缓冲模式）

emit(record):
  ├── 若 flush_later: record_buffer.append(record)        ← 只攒，不打印
  │   否则:           super().emit(record)                ← 正常打印
  └── 若 record.levelno >= ERROR:
        flush_buffer()   # 停 Live + 打印全部缓冲
        sys.exit(1)

flush_buffer():                ← 由 CLI 在 Live 退出后、或异常钩子调用
  ├── 若 live 存在且已启动: live.stop()   ← 先让终端脱离 Live 接管
  ├── flush_later = False                   ← 切到「正常打印」模式
  ├── 遍历 record_buffer: super().emit(record)   ← 把积压的日志逐条吐出
  └── record_buffer.clear()
```

调用时机有三处：

- **正常结束**：benchmark 双层循环跑完，`with dashboard.live:` 退出后，CLI 调 `delayed_log_handler.flush_buffer()`（[cli.py:540-541](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L540-L541)）。
- **未捕获异常**：`sys.excepthook` 里先记 ERROR，再 `flush_buffer()`（[logging.py:155-156](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L155-L156)）。
- **ERROR 日志**：handler 自己在 `emit` 里触发（[logging.py:72-74](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L72-L74)）。

#### 4.2.3 源码精读

**`DelayedRichHandler.emit`——缓冲为主，ERROR 立即触发**（[logging.py:63-74](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L63-L74)）：

```python
def emit(self, record: logging.LogRecord):
    if self.flush_later:
        self.record_buffer.append(record)   # 攒的是 LogRecord 对象，不是文本
    else:
        super().emit(record)                # flush 之后变成正常 handler

    if record.levelno >= logging.ERROR:
        self.flush_buffer()
        sys.exit(1)
```

注意它缓冲的是 **`LogRecord` 对象本身**，而不是渲染后的字符串。好处是：等会儿 flush 时，仍能走完整的 `RichHandler.emit`（带颜色、时间戳、traceback 高亮），渲染质量不打折。

**`flush_buffer`——先停 Live，再逐条吐**（[logging.py:76-84](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L76-L84)）：

```python
def flush_buffer(self):
    if self.live and self.live.is_started:
        self.live.stop()                    # ① 让终端脱离 Live 全屏接管
    self.flush_later = False                # ② 切换为「正常打印」模式
    for record in self.record_buffer:
        super().emit(record)                # ③ 把积压日志逐条用 RichHandler 渲染输出
    self.record_buffer.clear()
```

第 ① 步是点睛之笔：`live.stop()` 之后终端才恢复正常滚动模式，此时再 `super().emit()` 打印的日志才能正常留在终端历史里。顺序不能反——若不停 Live 就打印，日志照样被覆盖。`self.live.is_started` 的判空（[logging.py:77](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L77)）保证 Live 没启动时（比如出错发生在 `with` 之前）也不会报错。

**CLI 侧的收尾调用**（[cli.py:536-542](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L536-L542)）：`runner.cleanup()` 之后、生成报告之前，把积压日志一次性刷出来，再打「实验完成」。

#### 4.2.4 代码实践

**实践目标**：用一个「假的 Live」直观看到「缓冲期不打印、flush 后才吐出」的效果，从而回答「为什么 Live 上下文要延迟 flush」。

**操作步骤**（临时脚本 `tmp_delayed_demo.py`，**示例代码**）：

```python
# 示例代码
import logging
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from genai_bench.logging import DelayedRichHandler

layout = Layout(Panel("仪表盘占位", title="Dashboard"))
live = Live(layout, refresh_per_second=2, screen=True)

h = DelayedRichHandler(live=live)
logger = logging.getLogger("demo2")
logger.setLevel(logging.INFO)
logger.addHandler(h)

live.start()
print("← 这行会被 Live 覆盖，看不到")
for i in range(3):
    logger.info("during live %d" % i)   # 期望：这三条都不出现
live.stop()

print("=== Live 已停止，下面是 flush 出来的日志 ===")
h.flush_buffer()                        # 期望：此时才打印 during live 0/1/2
```

**需要观察的现象**：

1. Live 运行期间，屏幕只显示「Dashboard」面板；`during live 0/1/2` **看不到**，普通 `print` 也被覆盖。
2. `live.stop()` + `flush_buffer()` 之后，三条 `during live` 才依次出现在终端。

**预期结果**：这正是「延迟 flush」要解决的问题——Live 期间直接打印会丢失/刷屏，缓冲到 Live 结束再吐，才能保住完整日志。**待本地验证**：`screen=True` 在某些非交互终端（如 CI）下表现不同，建议在真实终端运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `flush_buffer` 里必须 **先 `live.stop()`、再** 遍历打印？

**参考答案**：因为 `live.stop()` 之前，终端仍被 Live 全屏接管，此时 `super().emit()` 打印的内容会被 Live 的下一次重绘覆盖，等于白打。只有先 `live.stop()` 让终端恢复正常滚动模式，后续打印的日志才能留在终端历史里。

**练习 2**：如果删掉 `emit` 里 `record.levelno >= logging.ERROR` 那段，会丢失什么能力？

**参考答案**：会丢失「出错即停、且错误可见」的能力。原本一条 ERROR 会立刻 `flush_buffer()`（停 Live、打出全部缓冲日志方便排错）再 `sys.exit(1)`；删掉后，ERROR 也只是被默默攒进缓冲，要等到实验正常结束才打印，且进程不会因 ERROR 退出——排错体验变差。

---

### 4.3 worker 日志队列：跨进程不污染终端

#### 4.3.1 概念说明

u7-l1 已经讲清了 worker 日志「从 worker 进程的 `WorkerRichHandler` → `worker_log_queue`（`multiprocessing.Queue`）→ master 的 `_consume_worker_logs` greenlet → master 的 logger」这条传输路径。本讲补上 `logging.py` 侧的三个关键细节，回答「worker 日志如何不污染各自终端」：

1. **worker 进程必须重写日志配置**。worker 是 fork 出来的，一出生就带着 master 的 handler（其中有引用 master `layout`/`live` 的 `RollingRichPanelHandler`/`DelayedRichHandler`）。若不重写，worker 的日志会去操作 master 进程的 layout 对象——既无意义、又可能因对象状态不一致而出错。`WorkerLoggingManager.setup_logging` 用 `logging.basicConfig(..., force=True)` 彻底重置根 logger（[logging.py:254-258](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L254-L258)），给 worker 换上一套「只写自己的文件 + 只往队列转发」的干净配置。

2. **worker 不直接打印，只往队列塞字典**。`WorkerRichHandler.emit` 把每条日志压成一个 `{"worker_id", "message", "level"}` 字典 `put` 进队列（[logging.py:97-105](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L97-L105)）。注意它转发的是 `record.getMessage()`（**已渲染的纯文本消息**），而不是 `LogRecord` 对象——因为 `LogRecord` 跨进程序列化（pickle）不可靠，而纯字符串/字典安全。这样 worker 进程 **本身不往终端写一个字**，终端输出权完全归 master 一家。

3. **worker 仍有自己的落盘文件**。`WorkerLoggingManager` 给每个 worker 配一个 `genai_bench_worker_{id}.log` 文件 handler（[logging.py:246-248](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L246-L248)）。所以即便 master 终端没显示某条 worker 日志，也能去对应 worker 文件里查。

「不污染各自终端」的完整含义：多个 worker 进程 + master 进程默认共享同一个终端（继承自 fork）。若每个 worker 都直接 print，多进程输出会交错穿插，还会和 master 的 `rich.Live` 仪表盘打架、刷成乱码。把所有 worker 日志收进队列、由 master 单点统一打印（带 `[Worker N]` 前缀），就彻底避免了这个问题。

#### 4.3.2 核心流程

worker 日志从产生到显示的完整链路（`logging.py` + `runner.py` 合看）：

```
[worker 进程，fork 自 master]
  _worker_process(worker_id):
    └─ WorkerLoggingManager(id, worker_log_queue, log_dir)   # runner.py:228-230
         └─ setup_logging(force=True):                        # 重写根 logger
              ├─ file_handler  → genai_bench_worker_{id}.log   （落盘）
              └─ WorkerRichHandler → 队列                       （转发）
    └─ 之后 worker 里任何 logger.info(...)
         └─ WorkerRichHandler.emit
              └─ worker_log_queue.put({worker_id, message, level})   # logging.py:97-105

[master 进程]
  _setup_master():                                           # runner.py:158-171
    └─ self.log_consumer = gevent.spawn(self._consume_worker_logs)
  _consume_worker_logs():                                     # runner.py:188-201
    └─ while True:
         if 队列空: gevent.sleep(0.1); continue
         log_data = queue.get_nowait()
         _process_log_data(log_data)                          # runner.py:173-186
           └─ logger.log(level, f"[Worker {id}] {message}")
                └─ 走 master 的 handler：面板 / 延迟缓冲 / 文件
```

要点：master 侧的 `logger`（[runner.py:22](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L22)，`init_logger(__name__)`）挂的是 master 自己的 handler，所以 worker 日志最终也进 master 的 Logs 面板/延迟缓冲/文件——和 master 自己的日志「同等待遇」，只是多了 `[Worker N]` 前缀。

#### 4.3.3 源码精读

**`WorkerRichHandler.emit`——只转发，不渲染到终端**（[logging.py:97-105](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L97-L105)）：

```python
def emit(self, record: logging.LogRecord):
    self.log_queue.put(
        {
            "worker_id": self.worker_id,
            "message": record.getMessage(),   # 渲染后的纯文本，跨进程安全
            "level": record.levelname,
        }
    )
```

它 **没有调用 `super().emit()`**，所以 rich 的彩色渲染、终端输出统统不发生——这正是「worker 不碰终端」的实现方式。

**`WorkerLoggingManager.setup_logging`——用 `force=True` 重写配置**（[logging.py:241-258](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L241-L258)）：

```python
logging.basicConfig(
    level=log_level,
    handlers=[file_handler, worker_handler],
    force=True,   # 关键：覆盖从 master fork 继承来的旧 handler 配置
)
```

`force=True` 是 Python 3.8+ `basicConfig` 的参数，会先移除根 logger 上所有已有 handler 再重新挂载。没有它，worker 会同时带着 master 的面板/延迟 handler（操作 master 的 layout/live）和自己新加的 handler，行为混乱。

**master 侧的消费 greenlet**（`runner.py`，u7-l1 已详述，这里只点关键行）：

- 队列在 `DistributedRunner.__init__` 创建：`self.worker_log_queue = multiprocessing.Queue()`（[runner.py:130](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L130)）。
- master 启动时派生消费 greenlet：`self.log_consumer = gevent.spawn(self._consume_worker_logs)`（[runner.py:171](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L171)）。
- `_consume_worker_logs` 空队列时 `gevent.sleep(0.1)` 让出 CPU（协作式调度，不阻塞其它 greenlet），非空则 `get_nowait()` 取出处理（[runner.py:188-201](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L188-L201)）。
- `_process_log_data` 对字典做空值兜底后，用 `getattr(logging, log_level)` 把字符串级别转成常量再打印，缺字段也不崩（[runner.py:173-186](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L173-L186)）。

> 与 u7-l1 呼应：master 还注册了一个名为 `worker_log` 的 Locust 消息 handler（`_create_log_handler`，[runner.py:309-327](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L309-L327)），但全仓库无 `send_message("worker_log", ...)`。真正生效的 worker 日志通道是上面这条 `multiprocessing.Queue`，不是 Locust 消息。读源码时要分清这两者。

#### 4.3.4 代码实践

**实践目标**：用最小多进程脚本复现「worker 只往队列塞字典、master 单点打印」的模式，理解为何这样不会污染终端。

**操作步骤**（临时脚本 `tmp_worker_demo.py`，**示例代码**）：

```python
# 示例代码
import logging
import multiprocessing
from genai_bench.logging import WorkerLoggingManager

def worker_target(worker_id, q):
    # 子进程里重写日志配置（force=True），之后只往 q 转发、不打印
    WorkerLoggingManager(str(worker_id), q, log_dir=None)
    log = logging.getLogger("w%d" % worker_id)
    for i in range(3):
        log.info("hello from worker %d #%d" % (worker_id, i))

if __name__ == "__main__":
    q = multiprocessing.Queue()
    procs = [multiprocessing.Process(target=worker_target, args=(i, q)) for i in range(3)]
    for p in procs: p.start()

    # master 模拟 _consume_worker_logs：单点消费
    import time
    time.sleep(0.5)
    while not q.empty():
        d = q.get_nowait()
        print("[Worker %s] %s" % (d["worker_id"], d["message"]))   # 带 [Worker N] 前缀

    for p in procs: p.join()
```

**需要观察的现象**：

1. 终端里只有 master 这一个 `print` 循环的输出，每条带 `[Worker N]` 前缀，**没有** 三个子进程各自的直接打印。
2. 当前目录下会生成 `genai_bench_worker_0.log` / `genai_bench_worker_1.log` / `genai_bench_worker_2.log` 三个文件，各含对应 worker 的日志。

**预期结果**：验证「worker 日志不直接落终端、而是经队列由单点统一打印 + 各自落盘文件」的设计。**待本地验证**：若并发打印出现轻微乱序属正常（多 worker 共享一个 master 打印循环），这正是项目里用「单 greenlet 串行消费」要规避的。

**源码阅读型实践（无需运行）**：阅读 [tests/distributed/test_runner.py:259-274](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/distributed/test_runner.py#L259-L274) 的 `test_log_consumer_processing`。它往 `worker_log_queue` 塞入「正常字典、`None`、缺字段字典」三类，再调 `_drain_log_queue`，断言 `_process_log_data` 对异常输入不崩溃——这是「master 消费侧的健壮性」的测试依据。

#### 4.3.5 小练习与答案

**练习 1**：`WorkerLoggingManager.setup_logging` 为什么必须传 `force=True`？去掉会怎样？

**参考答案**：worker 是 fork 自 master 的子进程，继承了一份 master 的根 logger 配置（含引用 master `layout`/`live` 的 `RollingRichPanelHandler`/`DelayedRichHandler`）。不 `force=True`，新 handler 只是「追加」，旧 handler 仍在——worker 日志会去操作 master 进程的 layout/live 对象，既无意义又可能出错。`force=True` 先清空旧 handler 再重挂，确保 worker 只有「写自己文件 + 往队列转发」两个 handler。

**练习 2**：`WorkerRichHandler.emit` 转发的是 `record.getMessage()`（字符串）而非 `record`（LogRecord 对象），为什么？

**参考答案**：跨进程传递需要经过 `multiprocessing.Queue` 的 pickle 序列化。`LogRecord` 对象携带 formatter、异常 traceback 等不易可靠 pickle 的内容，跨进程容易失败或失真；而 `record.getMessage()` 是已渲染好的纯字符串，配合 `worker_id`/`level` 组成普通字典，pickle 安全、传输稳定。

---

## 5. 综合实践

把三个模块串起来，做一次「端到端」的日志观察。建议用 u1-l2 的最小 text-to-text 基准命令（显式收窄为 1 场景 × 1 并发，避免跑太久），分别尝试两种配置：

**任务 A：观察「延迟 flush」与「Logs 面板」**

1. 用默认配置（`ENABLE_UI` 默认开）跑一次 benchmark。
2. 实验运行期间，观察终端：应当看到实时仪表盘，其中有一个红色边框、标题为 **Logs** 的面板在滚动显示最近日志；而终端本身 **没有** 散落的普通日志行。
3. 实验结束后（仪表盘消失），观察终端：之前被缓冲的日志此时 **一次性** 打印出来，最后是「🚀 The whole experiment has finished!」。
4. 打开实验目录（或 `--log-dir` 指定目录）下的 `genai_bench.log`，确认它从实验一开始就有完整记录（说明文件 handler 不受 Live 影响、始终落盘）。

**任务 B：观察「worker 日志不污染终端」**

1. 加 `--num-workers 2`（分布式）再跑一次。
2. 在 `--log-dir` 目录下应当出现 `genai_bench.log`（master）、`genai_bench_worker_0.log`、`genai_bench_worker_1.log`（各 worker）。
3. 在 master 终端 / Logs 面板里，应当能看到带 `[Worker 0]` / `[Worker 1]` 前缀的日志行——这就是 worker 日志经队列回传后、由 master 单点打印的结果；而 **worker 进程自己没有直接往这个终端写过任何东西**。
4. 用一句话回答本讲的核心问题：**为什么 Live 上下文要延迟 flush、worker 日志如何不污染终端？**（参考答案：Live 全屏重绘会覆盖/刷花直接打印的日志，故 master 用 `DelayedRichHandler` 缓冲到 Live 退出再吐、用 `RollingRichPanelHandler` 把实时日志写进面板；worker 则完全不直接打印，而是经 `multiprocessing.Queue` 把日志字典回传 master、由 master 单点渲染，从而多进程共享同一终端也不交错错乱。）

> 若没有可用的 OpenAI 兼容服务，任务 A/B 的「面板/终端现象」部分可改为阅读型：对照 [cli.py:412](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L412) 的 `with dashboard.live:` 与 [cli.py:540-541](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L540-L541) 的 `flush_buffer()`，口述一遍日志在 Live 进出前后的去向。**待本地验证**：实际终端表现依赖运行环境。

## 6. 本讲小结

- **三个 handler 各司其职**：`RollingRichPanelHandler` 把日志写进内存 buffer 再更新到 Logs 面板（绕开终端）；`DelayedRichHandler` 在 Live 期间缓冲 `LogRecord`、Live 退出后统一打印、遇 ERROR 立即 flush 退出；`WorkerRichHandler` 只往队列转发、不渲染到终端。
- **延迟 flush 的根因**：`rich.Live(screen=True)` 全屏重绘，会覆盖并刷花任何直接打印的日志。`flush_buffer` 必须 **先 `live.stop()` 让终端脱离 Live 接管，再** 逐条打印，顺序不可反。
- **日志永远落盘**：无论哪种命令、哪个进程，`LoggingManager`/`WorkerLoggingManager` 都先挂文件 handler，保证 `genai_bench.log` / `genai_bench_worker_{id}.log` 完整留底。
- **worker 不污染终端**：worker 用 `force=True` 重写日志配置（甩掉从 master fork 继承的面板/延迟 handler），只配「写自己文件 + 往队列转发」；`WorkerRichHandler` 转发的是渲染后的字符串字典（pickle 安全）；master 用单 greenlet 串行消费队列、加 `[Worker N]` 前缀统一打印。
- **健壮性细节**：异常钩子与 ERROR 日志都会触发 `flush_buffer`（判空 `delayed_handler`）；`_process_log_data` 对 `None`/缺字段字典兜底不崩；真正生效的 worker 日志通道是 `multiprocessing.Queue`，而非已注册却无发送方的 `worker_log` Locust 消息。

## 7. 下一步学习建议

- 本讲与 u7-l1（worker 日志的传输）、u7-l2（`Live` 仪表盘与 `with dashboard.live:`）构成「实时运行态」三连讲，建议三者对照阅读，把「日志/指标怎么在多进程 + Live 下流动」拼成完整画面。
- 接下来进入 **u8-l1 benchmark 主流程编排（capstone）**：那里会把本讲的 `LoggingManager` 构造、`with dashboard.live:` 包裹、`flush_buffer()` 收尾，与认证、采样、分布式、报告、上传等阶段串成一条完整时序，是贯穿全手册的总结篇。
- 若想动手扩展，可参考 u8-l3（扩展指南）思考：新增一个后端 User 时，它的日志会自动享受「面板 + 延迟缓冲 + worker 队列回传」三件套吗？（提示：只要用 `init_logger` 拿 logger，handler 由根 logger 统一挂载，无需额外配置。）
