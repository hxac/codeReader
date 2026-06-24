# 异步翻译与进度事件流

> 本讲承接 u2-l2。在上一讲里，我们把 `_do_translate_single` 这条同步主链路从头走到尾，看清了「打开 PDF → 修正 → 建 IL → 跑各 midend 阶段 → 渲染」的全部顺序。但那条链路是**同步阻塞**的——一旦启动，调用者只能干等它跑完才能拿到结果。
>
> 真实的翻译可能要几十秒到几分钟（要调 LLM、要跑模型）。下游 UI（比如 PDFMathTranslate-next 的网页）不可能让用户盯着一个冻住的界面。本讲就解决一个问题：**BabelDOC 如何把这条同步主链路包装成一条「边翻译、边吐进度」的异步事件流，供下游实时消费？**

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `high_level.async_translate` 如何用线程池执行同步的 `do_translate`，并把进度转成异步事件流；
- 默写出事件协议的五种事件（`progress_start` / `progress_update` / `progress_end` / `finish` / `error`）的字段与含义；
- 解释 `ProgressMonitor` 如何用「阶段权重 + 分片(part)」计算 `overall_progress`；
- 理解 `asynchronize.AsyncCallback` 为什么能把「同步回调」桥接成「异步迭代器」；
- 看懂 `main.create_progress_handler` 如何把事件渲染成 rich / tqdm 进度条，并能写出自己的事件消费者。

## 2. 前置知识

本讲用到几个 Python 并发概念，先用大白话过一遍：

- **同步 / 阻塞**：函数从头跑到尾，调用它的线程只能等。`do_translate` 就是这种——它内部要读写文件、调 LLM，期间不返回。
- **异步生成器（async generator）**：一种「按需产出值」的函数，用 `async def` + `yield` 定义。调用方用 `async for x in ag()` 一个一个地拿值，每次 `yield` 之间事件循环可以去干别的活。`async_translate` 就是异步生成器，产出的「值」就是进度事件。
- **事件循环（event loop）**：异步程序的「调度中枢」，决定现在跑哪个任务。主线程跑事件循环。
- **线程池（executor）**：一组备用工作线程。把一个同步阻塞函数丢进去，它就在工作线程里跑，不卡主线程的事件循环。
- **线程安全**：同一段数据被多个线程访问时，必须用专门的 API 操作。跨线程去碰事件循环，就得用 `call_soon_threadsafe` 这类「线程安全」入口，否则可能唤醒不了沉睡的循环。

一句话建立直觉：**BabelDOC 把同步翻译塞进线程池当「干活的工人」，主线程的事件循环当「播报员」，中间用 `AsyncCallback` 这个队列把工人的进度条子递给播报员，播报员再 `yield` 给你。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`babeldoc/format/pdf/high_level.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py) | 翻译主流程编排。本讲的「入口」`async_translate`、同步入口 `translate`、工人函数 `do_translate`、阶段表 `TRANSLATE_STAGES` 都在这里。 |
| [`babeldoc/progress_monitor.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py) | 进度监控器。`ProgressMonitor` 负责阶段权重、进度计算、分片(part)管理，并把进度通过回调发出去。 |
| [`babeldoc/asynchronize/__init__.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py) | 异步桥接。`AsyncCallback` 用一个 `asyncio.Queue` 把「线程池里的同步回调」变成「主线程里的异步迭代器」。 |
| [`babeldoc/main.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py) | CLI 入口。`main()` 消费 `async_translate` 的事件流，`create_progress_handler` 把事件渲染成 rich/tqdm 进度条。 |

四个文件恰好对应本讲的四个最小模块，它们的关系是：

```
main.async_for  ──消费──►  high_level.async_translate  ──yield 事件──►  你
                                  │ 桥接
                          asynchronize.AsyncCallback  ◄──回调写入──  ProgressMonitor
                                  │ run_in_executor
                          high_level.do_translate  （线程池里的工人，见 u2-l2）
```

## 4. 核心概念与源码讲解

### 4.1 async_translate 与事件协议

#### 4.1.1 概念说明

`do_translate`（u2-l2 讲过）是同步的，跑一次翻译可能几十秒。如果直接 `await do_translate(...)`，由于它根本不是协程，你既 await 不了，也拿不到中间进度。

`async_translate` 要解决两件事：

1. **不阻塞**：让 `do_translate` 在线程池里跑，主线程事件循环保持活力；
2. **报进度**：翻译过程中每个阶段的开始/推进/结束，都要变成一条条「事件」吐给调用方。

这两个目标合起来，就是一条「异步事件流」。下游（CLI 进度条、Web UI）只要 `async for event in async_translate(config)` 就能边等结果边刷新界面。

#### 4.1.2 核心流程

`async_translate` 的执行流程可以拆成 6 步：

1. 拿到当前事件循环，新建一个 `AsyncCallback`（带一个异步队列）和一个 `finish_event`、一个 `cancel_event`；
2. 用 `AsyncCallback` 的两个方法当回调，构造一个 `ProgressMonitor`——从此工人每报一次进度，就写进 `AsyncCallback` 的队列；
3. 用 `loop.run_in_executor(None, do_translate, pm, config)` 把**同步**的 `do_translate` 丢进线程池；
4. 主线程 `async for event in callback:` 从队列里取事件，`yield` 给调用方；
5. 收到 `error` 就 `break`；被取消（`CancelledError` / `KeyboardInterrupt`）就 `cancel_event.set()`；
6. 最后 `await finish_event.wait()`，等线程池里的工人彻底收尾（`on_finish`）再返回。

#### 4.1.3 源码精读

`async_translate` 是一个 `async def` + `yield` 的**异步生成器**，开头一大段 docstring 正是「事件协议」的权威说明：

[`babeldoc/format/pdf/high_level.py:299-346`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L299-L346) —— 这是事件协议的「合同」，五种事件的字段都写在这里。

函数体本身很短，但每一步都关键：

[`babeldoc/format/pdf/high_level.py:347-376`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L347-L376) —— 关键点逐条对照：

- `loop.run_in_executor(None, do_translate, pm, translation_config)`（[L361](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L361)）：`None` 表示用默认线程池。这一句是整套机制的「心脏」——同步函数 `do_translate` 从此在另一个线程里跑，返回一个 `future`。
- `async for event in callback:`（[L363](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L363)）：从 `AsyncCallback` 队列里取事件。`event = event.kwargs`（[L364](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L364)）说明每条事件其实是以关键字参数形式塞进队列的字典（如 `type="progress_update", stage=..., overall_progress=...`）。
- `if event["type"] == "error": break`（[L366-L367](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L366-L367)）：出错时退出消费循环，但不立刻 raise——后面还要等工人收尾。
- 取消处理（[L368-L372](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L368-L372)）：`CancelledError`（被外部取消）或 `KeyboardInterrupt`（Ctrl-C）时，`cancel_event.set()`，工人那边会通过它感知到取消。
- `if cancel_event.is_set(): future.cancel()`（[L373-L374](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L373-L374)）：尝试取消线程池任务（注意：正在跑的同步代码无法强制中断，真正生效靠工人在检查点读 `cancel_event`）。
- `await finish_event.wait()`（[L376](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L376)）：等工人走完 `finally` 里的 `pm.on_finish()`。这一句保证：**无论成功、出错还是被取消，函数返回前工人都已清理干净**，不会泄漏线程。

工人 `do_translate` 在三个时机通过 `pm` 触发事件（详见 u2-l2 与 4.2 节）：

- 正常完成 → `pm.translate_done(result)`（[L719](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L719)）→ 发 `finish` 事件；
- 抛异常 → `pm.translate_error(e)`（[L728](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L728)）→ 发 `error` 事件；
- 无论成败 → `finally` 里 `pm.on_finish()`（[L732](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L732)）→ 置位 `finish_event`，解除主线程的 `await`。

> 对比：还有一个**纯同步**入口 `translate()`（[`high_level.py:259-261`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L259-L261)），它也建 `ProgressMonitor` 但**不传回调、不进线程池**，直接返回 `TranslateResult`，没有事件流。需要进度就选 `async_translate`，只要结果就选 `translate`。

#### 4.1.4 代码实践

这是本讲的**主实践**：写一个最小脚本，把 `async_translate` 吐出的事件按 `type` 分类打印，重点观察 `progress_update` 里 `overall_progress` 的爬升。

> 技巧：用 `only_parse_generate_pdf=True` 可以**不调 LLM、不需要 API key**，只跑「解析→生成 PDF」，照样产生完整的 `progress_*` / `finish` 事件流，非常适合用来观察事件协议。

```python
# event_probe.py —— 观察 async_translate 的事件流（示例代码）
import asyncio
from babeldoc.format.pdf import high_level
from babeldoc.format.pdf.translation_config import TranslationConfig

# 解析模式下 translator / doc_layout_model 都不会被真正调用，
# 因此可以省去 API key，专心观察事件流。
config = TranslationConfig(
    translator=None,
    input_file="examples/ci/test.pdf",
    lang_in="en",
    lang_out="zh",
    doc_layout_model=None,
    only_parse_generate_pdf=True,   # 关键：跳过翻译，只走解析+生成
    use_rich_pbar=False,            # 关掉内置进度条，自己处理事件
    report_interval=0.1,
    no_dual=True,
)

async def main():
    counters = {}
    last_overall = None
    async for event in high_level.async_translate(config):
        t = event["type"]
        counters[t] = counters.get(t, 0) + 1
        if t == "progress_start":
            print(f"[start ] {event['stage']}  total={event['stage_total']}")
        elif t == "progress_update":
            overall = event["overall_progress"]
            if overall != last_overall:                       # 只在变化时打印
                print(f"[update ] {event['stage']} "
                      f"{event['stage_current']}/{event['stage_total']} "
                      f"overall={overall:.1f}")
                last_overall = overall
        elif t == "progress_end":
            print(f"[end   ] {event['stage']}  overall={event['overall_progress']:.1f}")
        elif t == "finish":
            print(f"[finish] {event['translate_result']}")
        elif t == "error":
            print(f"[error ] {event['error']}")
            break
        else:
            print(f"[{t}] {event}")          # 例如构造时发的 stage_summary
    print("事件计数：", counters)

asyncio.run(main())
```

**实践目标**：亲眼看到事件按 `progress_start → progress_update(多次) → progress_end` 的节奏交替出现，最后以 `finish` 收尾，并看到 `overall_progress` 从 0 单调爬向 100。

**操作步骤**：

1. 先确保资源就位：`babeldoc --warmup`（解析+生成 PDF 仍可能需要字体资源）。
2. 把上面脚本存为 `event_probe.py`，放在仓库根目录。
3. 运行 `python event_probe.py`。

**需要观察的现象**：

- 最先打印的往往是一条 `[stage_summary]`（在 4.2 会解释，它是 `ProgressMonitor` 构造时发的）；
- 随后是若干轮 `start/update/end`，每个 stage 一轮；
- `overall_progress` 在 `progress_update` 中递增，到 `progress_end` 时该阶段完成；
- 末尾一条 `finish`，且 `counters` 里 `progress_update` 数量远多于其它。

**预期结果 / 待本地验证**：`counters` 大致形如
`{'stage_summary': 1, 'progress_start': N, 'progress_update': M, 'progress_end': N, 'finish': 1}`，其中 `N` 为实际运行的阶段数，`M >> N`。具体数值取决于 `examples/ci/test.pdf` 的页数与 `report_interval`，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `async_translate` 不能直接 `await do_translate(...)`？

> **参考答案**：`do_translate` 是普通同步函数，`await` 它既不合法也无意义；而且它一旦运行就阻塞调用线程，期间无法 `yield` 进度。`run_in_executor` 把它丢到线程池，主线程事件循环才能一边从 `AsyncCallback` 队列消费事件、一边 `yield` 给调用方，实现「边翻译边报进度」。

**练习 2**：收到 `error` 事件后，`async_translate` 为什么是 `break` 而不是直接 `return` 或 `raise`？

> **参考答案**：`break` 只跳出消费循环，函数还会继续走到 `await finish_event.wait()`（[L376](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L376)）。这样能保证线程池里的 `do_translate` 走完 `finally`（`on_finish` + `cleanup_temp_files`）再彻底结束，避免工作线程残留、临时文件泄漏。

### 4.2 ProgressMonitor：阶段权重与分片(part)进度

#### 4.2.1 概念说明

`ProgressMonitor`（进度监控器）是事件流的「数据源」。它要回答两个问题：

1. **当前整体进度是多少？** 翻译有十几个阶段，每个阶段耗时不同，不能简单按「阶段数 / 总阶段数」算。
2. **分片(part)翻译时怎么算？** 当 `--max-pages-per-part` 把一份大 PDF 切成多片串行翻译（见 u2-l2 / u8-l2），每片都是一整套阶段，整体进度得把「第几片」算进去。

它的解法是：**给每个阶段一个相对耗时「权重」**，归一化后就是该阶段在总进度里的占比；分片时再加一个「片偏移」。

#### 4.2.2 核心流程

权重的来源是 `TRANSLATE_STAGES`（阶段表，第二列就是权重）：

[`babeldoc/format/pdf/high_level.py:60-75`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L75) —— 例如 `ILTranslator` 权重 46.96（最贵，要调 LLM），`AutomaticTermExtractor` 权重 30.0，解析 14.12。注意 `get_translation_stage`（[`L264-296`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L264-L296)）会按配置删掉不运行的阶段（如 `only_parse_generate_pdf` 会删掉全部翻译相关阶段），所以实际权重表是动态的。

归一化与进度计算的数学关系：

- 每个阶段归一化权重：
  \[
  w_i = \frac{\text{weight}_i}{\sum_j \text{weight}_j}
  \]
  即该阶段占总进度的比例，所有 \(w_i\) 之和为 1。
- 单阶段进度：\(\text{stage\_progress} = \text{stage\_current} / \text{stage\_total} \times 100\)。
- **整体进度**（含分片）由 `calculate_current_progress` 给出：
  \[
  \text{overall} = \underbrace{\text{completed\_parts} \times \frac{100}{\text{total\_parts}}}_{\text{part\_offset（已完成片的份额）}} + \underbrace{\text{part\_progress} \times \frac{100}{\text{total\_parts}}}_{\text{当前片内的加权进度}}
  \]
  其中 `part_progress` = 已完成阶段的 \(w_i\) 之和 + 当前进行中阶段的 \(w_i \times \text{stage\_progress}/100\)。

> 直觉版：把 0~100 的进度条按「片」等分，先填满前面已完成的片，再在当前片里按阶段权重往里填。

#### 4.2.3 源码精读

构造函数做归一化并把每个阶段包成 `TranslationStage`：

[`babeldoc/progress_monitor.py:13-70`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L13-L70) —— 重点看：
- `total_weight = sum(weight for _, weight in stages)`（[L35](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L35)）；
- `normalized_weight = weight / total_weight`（[L37](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L37)）；
- 构造末尾，**如果有回调，立刻发一条 `stage_summary`**（[L58-L70](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L58-L70)）——这解释了 4.1.4 实践里最先看到的那条 `[stage_summary]`，它把各阶段占比一次性告诉 UI。

三种进度事件分别由三个方法发出：

- `stage_start` → 发 `progress_start`：[`progress_monitor.py:110-131`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L110-L131)（核心在 [L120-L129](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L120-L129)）；
- `stage_update` → 发 `progress_update`（**带节流**）：[`progress_monitor.py:214-235`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L214-L235)；
- `stage_done` → 发 `progress_end`：[`progress_monitor.py:149-173`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L149-L173)（核心在 [L163-L173](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L163-L173)）。

进度计算分两层（注意 `part_offset` 的取法对父/子 monitor 不同）：

[`babeldoc/progress_monitor.py:175-212`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L175-L212) ——
- `calculate_current_progress`（[L175-L185](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L175-L185)）：父 monitor 用 `len(self.part_results)`（已完成的片数）当偏移，子（part）monitor 用 `self.part_index`；
- `_calculate_current_progress`（[L187-L212](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L187-L212)）：算「片内」进度，全部阶段完成时精确返回 100（[L195-L196](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L195-L196)）。

分片(part)由 `create_part_monitor` 创建子监控器，事件经 `_handle_part_progress` 回传给父监控器的回调：

[`babeldoc/progress_monitor.py:72-108`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L72-L108)。`do_translate` 在分片时正是用它给每片建监控器：`part_monitor = pm.create_part_monitor(i, len(split_points))`（[`high_level.py:654-656`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L654-L656)），并事先设 `pm.total_parts = len(split_points)`（[`L566`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L566)）。

收尾由 `on_finish` 负责——它正是主线程 `await finish_event.wait()` 的「解除者」：

[`babeldoc/progress_monitor.py:139-147`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L139-L147) —— `on_finish` 里 `self.loop.call_soon_threadsafe(self.finish_event.set)`（[L145](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L145)）跨线程把 `finish_event` 置位。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把 4.1.4 的脚本输出与权重表对上号。
2. **操作步骤**：
   - 打开 [`high_level.py:60-75`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L60-L75) 的 `TRANSLATE_STAGES`，手算 `only_parse_generate_pdf=True` 时剩余阶段的归一化权重（解析 14.12 / FontMapper 0.61 / PDFCreater 1.96 / SUBSET_FONT 0.92 / SAVE_PDF 6.34）；
   - 运行 4.1.4 脚本，记录每个 `progress_end` 时的 `overall_progress`；
   - 把实测增量和你手算的 \(w_i \times 100\) 比较。
3. **需要观察的现象**：某阶段结束时 `overall_progress` 的跳变量，应近似等于该阶段归一化权重 × 100。
4. **预期结果**：例如「解析」阶段结束时应跳约 \(14.12/23.95 \times 100 \approx 58.9\)（分母为剩余五项权重和）。**待本地验证**（取决于 `only_parse_generate_pdf` 实际保留的阶段）。

#### 4.2.5 小练习与答案

**练习 1**：`TRANSLATE_STAGES` 里的数字（如 46.96）是什么含义？归一化后 `ILTranslator` 占总进度多少？

> **参考答案**：是相对耗时「权重」。全部权重之和约 130 左右，`ILTranslator` 归一化后约占 \(46.96/130 \approx 36\%\)，是占比最大的阶段——这也印证了「调 LLM 是主要瓶颈」。

**练习 2**：分片翻译时，第 2 片（`part_index=1`，共 3 片）刚启动时 `overall_progress` 大约是多少？

> **参考答案**：约 33.3。因为 `part_offset = part_index × 100/total_parts = 1 × 100/3 ≈ 33.3`，新片内进度还没开始，所以整体进度直接从「已完成 1 片」的份额起步。

### 4.3 AsyncCallback：同步回调到异步迭代器的桥接

#### 4.3.1 概念说明

现在有一个「跨线程」的鸿沟：

- **写事件的人**（`ProgressMonitor` 的回调）跑在**线程池的工作线程**里（因为 `do_translate` 在线程池）；
- **读事件的人**（`async for event in callback`）跑在**主线程的事件循环**里。

`AsyncCallback` 就是横跨这道鸿沟的桥。它内部放一个 `asyncio.Queue`：工作线程往里 `put`，主线程从里 `get`。它还实现 `__aiter__` / `__anext__`，于是能直接用在 `async for` 里。

#### 4.3.2 核心流程

```
工作线程                          主线程事件循环
ProgressMonitor 回调              async for event in callback:
   │ step_callback(...)              │ await queue.get() → event
   ▼                                  │  yield event
loop.call_soon_threadsafe(           │
   queue.put_nowait, args)  ──写入──► asyncio.Queue
                                      │
finished_callback(...)               当 finished 且 queue 为空:
   先 step 再 finished=True           raise StopAsyncIteration
```

关键点：写入用 `call_soon_threadsafe`（线程安全地唤醒事件循环）；结束标记 `finished` 要在「最后一个事件入队之后」才置位。

#### 4.3.3 源码精读

整个文件很短，核心是 `AsyncCallback`：

[`babeldoc/asynchronize/__init__.py:11-51`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py#L11-L51)。逐点看：

- 构造时抓住当前事件循环 `self.loop = asyncio.get_event_loop()`（[L15](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py#L15)），后面跨线程要用。
- `step_callback`（[L17-L26](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py#L17-L26)）：把参数包成 `Args`，用 `self.loop.call_soon_threadsafe(self.queue.put_nowait, args)` 线程安全地入队；末尾 `time.sleep(0.01)`（[L26](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py#L26)）主动让出 GIL，给事件循环处理消息的机会。**注意：它只入队，不置 `finished`**，所以 `__anext__` 不会提前结束。
- `finished_callback`（[L28-L34](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py#L28-L34)）：先调一次 `step_callback`（把 `finish`/`error` 事件也入队），**再**置 `self.finished = True`；且若已 finished 直接返回，保证幂等。
- `__anext__`（[L44-L51](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py#L44-L51)）：只有当「已 finished **且** 队列为空」才 `raise StopAsyncIteration`，否则 `await self.queue.get()` 取下一条。这个「与」条件保证：即使结束信号先到，队列里残留的事件也会被消费干净。

#### 4.3.4 代码实践（源码阅读 + 微实验）

1. **实践目标**：亲手验证「先入队再置 finished」的必要性。
2. **操作步骤**：在 Python 里写一段示例代码（非项目原有，已标注）：

   ```python
   # 示例代码：演示 AsyncCallback 的结束语义
   import asyncio, threading
   from babeldoc.asynchronize import AsyncCallback

   async def consumer(cb):
       async for item in cb:
           print("got:", item.kwargs)

   cb = AsyncCallback()
   t = asyncio.create_task(consumer(cb))

   def worker():
       cb.step_callback(type="progress_update", stage="A", overall_progress=10)
       cb.step_callback(type="progress_update", stage="A", overall_progress=20)
       cb.finished_callback(type="finish")  # 先 step，再 finished=True

   threading.Thread(target=worker).start()
   ```

3. **需要观察的现象**：即使 `finished_callback` 标记了结束，前面两条 `progress_update` 也一定会被打印，最后才打印 `finish`。
4. **预期结果**：输出顺序为 `overall_progress=10` → `20` → `finish`，没有事件丢失。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`step_callback` 为什么必须用 `call_soon_threadsafe` 而不是直接 `self.queue.put_nowait(args)`？

> **参考答案**：`step_callback` 在线程池的工作线程里被调用，而消费方在主线程事件循环里。直接 `put_nowait` 虽然队列本身线程安全，但**无法唤醒正在 `await` 的事件循环**——循环可能正睡着等别的任务。`call_soon_threadsafe` 是跨线程操作事件循环的标准入口，能可靠地把「入队」这件事排进循环、唤醒 `queue.get()`。

**练习 2**：如果 `finished_callback` 里把 `self.finished = True` 放到 `step_callback` **之前**会怎样？

> **参考答案**：可能丢失最后的 `finish`/`error` 事件。因为置位后，若 `__anext__` 恰好在事件入队前被调用、又恰好看到队列为空，就会立刻 `StopAsyncIteration` 结束迭代。所以源码坚持「先入队、再置位」（[L33-L34](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/asynchronize/__init__.py#L33-L34)）。

### 4.4 create_progress_handler：把事件流渲染成进度条

#### 4.4.1 概念说明

事件流是「机器友好」的字典，但终端用户要看的是「人友好」的进度条。`main.create_progress_handler` 就是把前者翻译成后者的「渲染器」。它根据 `config.use_rich_pbar` 选用两种实现：

- `True`（默认）：用 `rich.Progress`，给每个阶段建一条独立子进度条，外加一条总进度条；
- `False`：用 `tqdm`，只有一条总进度条，按 `overall_progress` 增量更新。

#### 4.4.2 核心流程

```
main():
  progress_context, progress_handler = create_progress_handler(config)
  with progress_context:                         # 启动进度条
      async for event in async_translate(config):
          progress_handler(event)                # 把事件喂给渲染器
          if event["type"] in ("error","finish"): break
```

渲染器内部按 `event["type"]` 分派：`progress_start` 建子任务（rich）/忽略（tqdm）、`progress_update` 更新数值、`progress_end` 标记完成。

#### 4.4.3 源码精读

`main()` 消费事件流的循环：

[`babeldoc/main.py:739-755`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L739-L755) —— `create_progress_handler` 返回 `(context, handler)`；`with progress_context:` 管理进度条生命周期；`async for event in async_translate(config): progress_handler(event)` 把每条事件交给渲染器；遇到 `error`/`finish` 即 `break`。

`create_progress_handler` 的 rich 分支（每个 stage 一条子进度条 + 一条总进度条）：

[`babeldoc/main.py:799-851`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L799-L851) ——
- `progress.add_task("translate", total=100)`（[L807](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L807)）建总进度条；
- `progress_start` 时为新 stage 建子任务（[L813-L818](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L813-L818)）；
- `progress_update` 同时更新子任务和总进度条（`completed=event["overall_progress"]`，[L819-L833](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L819-L833)）；
- `progress_end` 把子任务填满（[L834-L849](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L834-L849)）。

tqdm 分支（单条进度条，只认 `overall_progress`）：

[`babeldoc/main.py:852-865`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L852-L865) —— 注意它**只处理 `progress_update` 和 `progress_end`**，不处理 `progress_start`（因为只有一条条，没必要为每个阶段建任务）；`pbar.update(event["overall_progress"] - pbar.n)` 用差量更新，避免重复累加。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：对比两种渲染器对同一事件流的不同表现。
2. **操作步骤**：
   - 在 4.1.4 脚本的 `TranslationConfig(...)` 里分别设 `use_rich_pbar=True` 与 `False`，但**不调用** `create_progress_handler`（让你自己的 `main()` 仍是消费者）；改为另外写一段：直接调用 `create_progress_handler(config)` 拿到 `handler`，再把你从 `async_translate` 收到的事件喂给它。
   - 示例（示例代码）：

     ```python
     ctx, handler = create_progress_handler(config, show_log=False)
     with ctx:
         async for event in high_level.async_translate(config):
             handler(event)
             if event["type"] in ("error", "finish"):
                 break
     ```
3. **需要观察的现象**：`use_rich_pbar=True` 时终端出现多条阶段条 + 一条总条；`False` 时只有一条 `tqdm` 总条，描述行随阶段切换。
4. **预期结果**：两种模式最终总进度都到 100；rich 模式信息更细。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：tqdm 分支为什么不处理 `progress_start`？

> **参考答案**：tqdm 只维护一条总进度条，靠 `overall_progress` 的差量更新即可；它不需要为每个阶段建独立任务，所以 `progress_start` 对它没有用处，自然不处理。rich 分支才需要借 `progress_start` 给每个阶段开一条子任务。

**练习 2**：`pbar.update(event["overall_progress"] - pbar.n)` 为什么要减去 `pbar.n`？

> **参考答案**：`tqdm.update(n)` 是「增量 n」而非「设到 n」。`overall_progress` 是绝对值，所以要减去当前已显示的 `pbar.n` 得到本次增量，否则进度会被反复累加、迅速爆表。

## 5. 综合实践

把四个模块串起来，做一个「自定义事件消费者」：

> **任务**：实现一个纯文本进度条 + JSONL 事件日志双输出的消费者，并用它对比 `report_interval` 对事件数量的影响。

要求：

1. 复用 4.1.4 的 `config`（`only_parse_generate_pdf=True`，免 API key）。
2. 自己写一个 `consume(config)` 协程，对 `async_translate` 的每个事件：
   - 把 `overall_progress` 渲染成一行 40 字符的文本进度条（如 `[████████████░░░░░░░░░░░░░░░░░░░] 30.0%`，用 `\r` 原地刷新）；
   - 同时把整条事件 `json.dumps` 追加写入 `events.jsonl`。
3. 分别用 `report_interval=0.1` 和 `report_interval=1.0` 各跑一次，统计 `events.jsonl` 里 `progress_update` 的条数。

**需要观察与解释**：

- `report_interval` 越大，`progress_update` 越少——因为 `ProgressMonitor.stage_update` 的节流逻辑（[`progress_monitor.py:217-219`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/progress_monitor.py#L217-L219)）：距上次上报不足 `report_interval` 且 `stage.total > 3` 时直接 return。
- 这条实践同时验证了三个模块：事件协议（你消费的就是它）、`ProgressMonitor` 的节流（`report_interval` 生效）、`AsyncCallback` 的桥接（事件能稳定跨线程到达你的协程）。

**预期结果 / 待本地验证**：`report_interval=1.0` 的 `progress_update` 条数应明显少于 `0.1`；两个配置的 `overall_progress` 最终都到达 100。具体数值**待本地验证**。

## 6. 本讲小结

- `async_translate` 是**异步生成器**，用 `loop.run_in_executor` 把同步的 `do_translate` 丢进线程池，自己一边从 `AsyncCallback` 取事件一边 `yield`，从而「边翻译边报进度」。
- 事件协议有五种核心事件：`progress_start` / `progress_update` / `progress_end` / `finish` / `error`；此外 `ProgressMonitor` 构造时还会发一条 `stage_summary` 告知各阶段占比。
- `ProgressMonitor` 用**阶段权重**算进度：`TRANSLATE_STAGES` 第二列是相对耗时权重，归一化后即占比；分片(part)时再加 `part_offset`（已完成片 × 100/总片数）。
- `AsyncCallback` 是跨线程桥梁：工作线程用 `call_soon_threadsafe` 把事件塞进 `asyncio.Queue`，主线程 `async for` 取出；「先入队、再置 `finished`」保证不丢最后事件。
- 取消与收尾靠两个 `Event`：`cancel_event`（`threading.Event`）让工人感知取消，`finish_event`（`asyncio.Event`）由 `on_finish` 跨线程置位、解除主线程的 `await`，保证线程与临时文件都被清理。
- `create_progress_handler` 把事件渲染成进度条：`use_rich_pbar=True` 用 rich（每阶段一条子条 + 总条），`False` 用 tqdm（单条按 `overall_progress` 差量更新）。

## 7. 下一步学习建议

- 想看清「工人」`do_translate` 内部到底按什么顺序调各阶段、分片时如何切？这是 u2-l2 的内容，建议结合本讲再读一遍 [`high_level.py:527-733`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L527-L733)。
- 对分片(part)机制（`SplitManager` / `create_part_monitor` / `ResultMerger`）感兴趣，继续学 **u8-l2 分片翻译与结果合并**。
- 想了解另一套「外部 RPC 服务」如何复用这套进度事件流（NDJSON 协议），继续学 **u8-l4 executor RPC 服务**。
- 想深入取消/异常的健壮性（`on_finish`、`safe_save`、`translate_error` 回退），继续学 **u8-l5 异常体系与健壮性处理**。
