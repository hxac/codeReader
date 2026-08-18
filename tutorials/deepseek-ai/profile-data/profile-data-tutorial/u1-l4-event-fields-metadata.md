# u1-l4 读懂单条事件：ph/cat/pid/tid/ts/dur 与元数据事件

## 1. 本讲目标

u1-l3 看清了轨迹文件的「封面」（顶层字段），本讲放大到 `traceEvents` 数组里的**单条事件**。读完本讲，你应该能够：

1. 说出一条事件对象里八个常用字段 `ph` / `cat` / `name` / `pid` / `tid` / `ts` / `dur` / `args` 各自回答什么问题。
2. 解释 `ph`（phase，阶段）的取值含义：`X` 完成事件、`M` 元数据事件、`s`/`f` 流事件、`i` 即时事件——本仓库三份轨迹实际只出现这五种。
3. 解释 `cat`（category，类别）的含义，列出 train.json 中实际出现的类别，并报出每类的全量计数。
4. 用 `ph:"M"` 元数据事件（`process_name` / `process_labels` / `thread_name` 等）把数字 `pid`/`tid` 翻译成「CPU」「GPU 0」「stream 7」这样的人类可读名字。
5. 用 `ts` 与 `dur`（微秒）计算事件区间 \([ts,\ ts+dur]\)，并亲手验证 X 事件在同一轨道上的**嵌套**关系。

本讲的所有数字都来自对 train.json 的逐类文本统计（作者已用正则搜索核实，方法在 4.3.4 交代），读者运行脚本应得到完全相同的结果。

## 2. 前置知识

- **回顾 u1-l3**：轨迹顶层是 `schemaVersion` / `deviceProperties` / `distributedInfo` / `traceEvents`（+ `traceName`）；train.json 有 14240 条事件、97654 行多行排版；`ts` 是 Unix 纪元**微秒**；数组顺序 ≠ 时间顺序。
- **进程与线程**：操作系统里进程（process）是资源分配单位，线程（thread）是调度执行单位。Linux 给每个进程一个 PID、给其中每个线程一个 TID，且**主线程的 TID 恰好等于进程的 PID**——记住这一点，后面看到 `pid: 1166, tid: 1166` 就不会奇怪。
- **CPU 与 GPU 的异步关系**（本讲最重要的直觉）：CPU 上的 PyTorch 调用一个算子（如 `aten::empty`）时，并不会马上让 GPU 干活，而是调用 CUDA 运行时 API（如 `cudaLaunchKernel`）把内核**提交**给 GPU 的某个流（stream，GPU 上的任务队列），然后继续往下跑；GPU 稍后在流上**真正执行**这个内核。所以同一次内核启动会在轨迹里留下**两段记录**：CPU 侧一条 `cuda_runtime`、GPU 侧一条 `kernel`，时间上 GPU 那条晚于 CPU 那条。
- **完成事件的嵌套**：Chrome Trace 里 `ph:"X"` 事件按时间在同一 `pid`/`tid` 上可互相包含——外层事件区间完全覆盖内层事件区间，查看器据此画出「大块套小块」的层级（如 `ProfilerStep#1` 套住 `1F1B` 再套住 `combine(B)`）。
- **流（flow）事件**：`ph:"s"`（start）与 `ph:"f"`（finish）成对出现，靠相同的 `id` 字段配对，查看器在两条轨道之间画一条**箭头**。它不表示耗时，只表示「这两件事有关系」。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| [train.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json)（97654 行） | 唯一精读对象：所有事件样例与全量统计都出自它 |
| [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg) | 截图对照：本讲讲完字段后，图中每条色块都能对上一个字段组合 |

本讲不引用 prefill.json / decode.json 的具体行号（decode 是单行文件无法按行引用），但讲义末尾的脚本对三份文件通用。

## 4. 核心概念与源码讲解

本讲的最小模块：

1. 一条事件的解剖：八个字段各回答什么问题
2. `ph` 与流事件：X / M / s / f / i 五种阶段
3. `cat`：事件类别速查表与全量统计
4. 元数据事件：把 pid/tid 变成 CPU、GPU 0 与 stream 7

### 4.1 一条事件的解剖：八个字段各回答什么问题

#### 4.1.1 概念说明

`traceEvents` 里每个元素都是一个 JSON 对象，描述「某个轨道上、某段时间内、发生的一件事」。八个常用字段：

| 字段 | 回答的问题 | 取值示例 |
|---|---|---|
| `ph` | 这是哪种事件？（phase，阶段） | `"X"` / `"M"` / `"s"` / `"f"` / `"i"` |
| `cat` | 属于哪一类？（category，类别） | `"cpu_op"` / `"kernel"` / … |
| `name` | 具体是什么事？ | `"aten::empty"`、`"cudaLaunchKernel"` |
| `pid` | 在哪个进程（哪条进程轨道）？ | `1166`（CPU）、`0`（GPU 0） |
| `tid` | 在该进程的哪个线程/流上？ | `2571`、`7`（stream 7） |
| `ts` | 何时开始？（Unix 纪元微秒） | `1740461679479712` |
| `dur` | 持续多久？（微秒） | `85` |
| `args` | 附带的详细参数（对象，各类事件不同） | 见 4.3.3 |

两条常用公式：

\[ \text{事件区间} = [ts,\ ts + dur]\ (\mu s) \]

\[ \text{嵌套判定}:\ ts_{outer} \le ts_{inner}\ \text{且}\ ts_{inner} + dur_{inner} \le ts_{outer} + dur_{outer}\ (\text{同一 } pid/tid) \]

`pid`/`tid` 本身只是数字编号（甚至可能是字符串，见 4.4.3 的 `pid: "Spans"`），它们的意义完全由元数据事件赋予——这正是本讲第 4 个模块的主题。

#### 4.1.2 核心流程

读一条事件的推荐顺序：

```text
拿到一个事件对象 e
  ├─ 1. e["ph"]   → 先判断类型：X 才有 dur，M 是命名标签，s/f 是箭头
  ├─ 2. e["cat"]  → 判断类别：在 CPU 上？GPU 上？还是纯连线？
  ├─ 3. e["pid"]/e["tid"] + 元数据映射 → 定位到 "CPU 主线程" / "GPU 0 的 stream 7"
  ├─ 4. e["ts"]/e["dur"] → 算出区间，与同轨道其他事件比嵌套
  └─ 5. e["args"] → 取细节（correlation、stream、grid、bytes …）
```

#### 4.1.3 源码精读

train.json 的第一条事件（u1-l3 已见过开头，这次逐字段读）：

[train.json:L72-L78](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L72-L78) —— 一个 `ph:"X"`、`cat:"cpu_op"` 的完成事件：`name` 是 `autograd::engine::evaluate_function: BackwardWithAuxiliaryLossBackward`（autograd 引擎执行反向节点），位于 `pid 1166 / tid 2571`，区间 \([1740461679479712,\ 1740461679479797]\) 微秒；`args` 里的 `External id: 5633` 与 `Ev Idx: 0` 是事件编号，`Fwd thread id` 与 `Sequence number` 只出现在反向相关算子上，用于回指它的前向调用。

接下来三连事件演示**嵌套**：

[train.json:L86-L106](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L86-L106) —— `aten::ones`（造全 1 张量，ts=…738、dur=30，区间 [738, 768]）内部套着 `aten::empty`（[741, 751]）与 `aten::fill_`（[752, 767]）：先分配内存、再填充值，两个子算子区间都被父算子覆盖，查看器里画成「大块套两个小块」。

注解事件同样嵌套，而且规模大得多：

[train.json:L14416-L14436](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14416-L14436) —— `ProfilerStep#1`（区间长 112202 µs ≈ 112.2 ms）套住 `1F1B`（112168 µs），`1F1B` 又套住第一个 `combine(B)`（626 µs）。这就是 u1-l2 截图里「1F1B 大括号」的数据来源，细读留给 u2-l2。

#### 4.1.4 代码实践

**实践目标**：亲手把一条事件的八个字段拆开，并用 ts/dur 验证嵌套。

**操作步骤**（示例代码）：

```python
# dissect_event.py —— 示例代码
import json

with open("train.json", "r", encoding="utf-8") as f:
    evts = json.load(f)["traceEvents"]

e = evts[0]
for k in ["ph", "cat", "name", "pid", "tid", "ts", "dur", "args"]:
    print(f"{k:5} = {e.get(k)}")
print("区间 =", [e["ts"], e["ts"] + e["dur"]], "µs")

# 找到 aten::ones / aten::empty / aten::fill_ 三兄弟并手算嵌套
for ev in evts:
    if ev.get("name") in ("aten::ones",) and ev["pid"] == 1166 and ev["tid"] == 2571:
        ones = ev
        outer = (ones["ts"], ones["ts"] + ones["dur"])
        inner = [(x["name"], x["ts"], x["ts"] + x["dur"])
                 for x in evts
                 if x.get("cat") == "cpu_op" and x["pid"] == 1166 and x["tid"] == 2571
                 and outer[0] <= x["ts"] and x["ts"] + x["dur"] <= outer[1]
                 and x is not ones]
        print("aten::ones 区间:", outer)
        for row in inner:
            print("  内含:", row)
        break
```

**需要观察的现象**：第一条事件打印出的字段与 4.1.3 的行号引用是否一致？`aten::ones` 的区间内是否恰好落进两个子算子？

**预期结果**：首事件为 `ph=X, cat=cpu_op, name=autograd::engine::evaluate_function: BackwardWithAuxiliaryLossBackward, pid=1166, tid=2571, ts=1740461679479712, dur=85`；`aten::ones` 区间 `[1740461679479738, 1740461679479768]`，内含 `aten::empty`（…741→…751）与 `aten::fill_`（…752→…767）两条（数字与 4.1.3 一致；本讲义作者已按行核对，运行结果若有出入请先检查文件是否完整）。

#### 4.1.5 小练习与答案

**练习 1**：`ProfilerStep#1` 的 `ts=1740461679470813, dur=112202`，它的结束时刻是多少？`1F1B`（ts=…833, dur=112168）结束时刻又是多少？

**参考答案**：`ProfilerStep#1` 结束于 \(1740461679470813 + 112202 = 1740461679583015\) µs；`1F1B` 结束于 \(1740461679470833 + 112168 = 1740461679583001\) µs，比外层早 14 µs 结束——嵌套成立，且外层恰好比内层「宽」一点点（开始早 20 µs、结束晚 14 µs）。

**练习 2**：为什么 `args` 要设计成「因 cat 而异」而不是统一格式？

**参考答案**：不同类别的事件携带的细节天然不同——CPU 算子关心算子编号，CUDA API 关心 `correlation`，GPU 内核关心 `stream`/`grid`/`block`，拷贝关心 `bytes`。统一格式要么塞满空字段、要么丢失细节；`args` 作为自由字典让每类事件自带最相关的参数。

**练习 3**：一个 `ph:"M"` 元数据事件有 `dur` 吗？

**参考答案**：没有。`M` 事件只携带「名字/排序」标签（见 4.4），`ts` 只是记录写入时刻，不表示任何耗时区间；只有 `ph:"X"` 完成事件才有 `dur`。

### 4.2 ph 与流事件：X / M / s / f / i 五种阶段

#### 4.2.1 概念说明

`ph` 是 Chrome Trace Event 格式里的 phase（阶段）标记，决定这条事件的「语法」。Chrome 格式还定义了其他阶段（如 `b`/`e` 异步区间），但**本仓库三份轨迹实际只出现五种**：

| `ph` | 名称 | 有无 `dur` | 作用 |
|---|---|---|---|
| `"X"` | 完成事件（Complete） | 有 | 一段有起止的活动，时间线的「色块」，可嵌套 |
| `"M"` | 元数据事件（Metadata） | 无 | 给进程/线程命名、排序（4.4 专讲） |
| `"s"` | 流开始（flow start） | 无 | 一条箭头的起点，靠 `id` 与 `f` 配对 |
| `"f"` | 流结束（flow finish） | 无 | 一条箭头的终点；常见附加字段 `bp:"e"`（binding point，绑定到所在切片的末尾） |
| `"i"` | 即时事件（Instant） | 无 | 时间轴上的一个「时刻戳记」，如 `Record Window End` |

train.json 里两种 `cat` 的流事件最值得认识：

- **`cat:"ac2g"`**（名字直译「async CPU-to-GPU」）：把 CPU 侧的 CUDA API 调用与 GPU 侧的实际执行连成箭头——u1-l2 里你在时间线上看到的那些斜跨 CPU/GPU 轨道的连线就是它。它也是全文件数量最多的一类（4876 条，见 4.3），因为每次 CUDA API 调用与每个 GPU 活动都要挂箭头。
- **`cat:"fwdbwd"`**：PyTorch autograd 用来把**前向算子与对应的反向算子**绑定的流事件，让查看器能从反向跳回前向。train.json 中仅 68 条，全部在 CPU 的 tid 2571 上。

#### 4.2.2 核心流程

以一次内核启动为例，按**时间顺序**产生的四条相关事件：

```text
CPU 主线程 (pid 1166, tid 1166)              GPU 0 / stream 27 (pid 0, tid 27)
────────────────────────────────              ─────────────────────────────────
ts=…471323  cudaLaunchKernel (X, cuda_runtime)
ts=…471323  ac2g 流开始 (s, id=44479)  ───────┐
                                              │ 箭头
                                              └→ ts=…471346  kernel (X) 开始执行
                                                  ts=…471346  ac2g 流结束 (f, id=44479)
```

要点：

1. `s` 事件的 `ts` 等于 CPU API 调用时刻，`f` 事件的 `ts` 等于 GPU 活动开始时刻，两者之差就是「提交到执行」的滞留时间：

   \[ \Delta t = ts_{f} - ts_{s} = ts_{GPU} - ts_{CPU} \]

2. **文件顺序再次背叛时间顺序**：这四条事件在 train.json 里以 kernel → f → cudaLaunchKernel → s 的顺序排列（GPU 轨道的事件整块提前了），同一个 correlation 组内部也要靠字段而非位置还原因果。

#### 4.2.3 源码精读

correlation 编号 44479 的四件套（本讲先认识 `ph`，`correlation` 字段的完整用法留给 u2-l1）：

[train.json:L31247-L31262](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31247-L31262) —— GPU 侧 `ph:"X"`、`cat:"kernel"` 事件：FP8 量化内核 `cuda::per_token_cast_to_fp8_with_channels`，`pid 0, tid 27`（GPU 0 的 stream 27），`ts=…471346, dur=52`；`args` 携带 `stream: 27`、`correlation: 44479`、`grid [4096,1,1]`、`block [128,1,1]`、`registers per thread: 31`、`est. achieved occupancy %: 100` 等执行细节。

[train.json:L31263-L31266](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31263-L31266) —— 紧随内核的 `ph:"f"` 流事件：`id: 44479`、`pid 0, tid 27`、`ts` 与内核开始时刻相同，`cat:"ac2g"`、`bp:"e"`。这是箭头的 GPU 端。

[train.json:L31267-L31274](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31267-L31274) —— CPU 侧 `ph:"X"`、`cat:"cuda_runtime"` 事件：`cudaLaunchKernel`，`pid 1166, tid 1166`（CPU 主线程），`ts=…471323, dur=22`；`args` 里 `correlation: 44479` 与上面内核一致（`cbid: 211` 是 CUDA API 编号）。

[train.json:L31275-L31278](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31275-L31278) —— 箭头的 CPU 端：`ph:"s"`、`id: 44479`、`pid 1166, tid 1166`、`ts=…471323`（与 cudaLaunchKernel 同刻）。

注意内核的 `args` 里 `External id: 44479` 与 `correlation: 44479` 恰好同值——这是编号计数器在此处的巧合，两者语义并不相同（前者指向 CPU 算子、后者配对 CUDA API 与 GPU 活动），不要混用，u2-l1 会专门区分。

`fwdbwd` 的样例（都在 tid 2571 上）：

[train.json:L1080-L1083](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L1080-L1083) —— `ph:"s"`、`cat:"fwdbwd"`、`id: 2`：前向侧的流起点。

[train.json:L2425-L2428](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L2425-L2428) —— `ph:"f"`、`cat:"fwdbwd"`、`id: 1`、`bp:"e"`：某个反向侧的流终点，与同 `id` 的前向起点配对。

文件末尾的两条 `ph:"i"` 即时事件：

[train.json:L97644-L97651](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97644-L97651) —— `Iteration Start: PyTorch Profiler`（`pid:"Traces"`）与 `Record Window End`（`pid:""`，ts=…603007）：采集窗口的开始/结束戳记，没有 `dur`，也不是任何轨道上的活动。

#### 4.2.4 代码实践

**实践目标**：用 `ph`/`id` 字段亲手把箭头配对，量化「提交→执行」的滞留。

**操作步骤**（示例代码）：

```python
# pair_flows.py —— 示例代码
import json

with open("train.json", "r", encoding="utf-8") as f:
    evts = json.load(f)["traceEvents"]

flows = {}
for ev in evts:
    if ev.get("cat") == "ac2g" and ev["ph"] in ("s", "f"):
        flows.setdefault(ev["id"], {})[ev["ph"]] = ev

for fid in (44479, 45234):
    s, f = flows[fid]["s"], flows[fid]["f"]
    print(f"id={fid}: CPU {s['ts']} -> GPU {f['ts']}, "
          f"滞留 {f['ts'] - s['ts']} µs")
```

**需要观察的现象**：两个 `id` 的滞留时间差多少个数量级？

**预期结果**：`id=44479`（内核）滞留 \(471346 - 471323 = 23\) µs；`id=45234`（一次 `Memcpy DtoD`，见 4.3.3）滞留 \(476346 - 474038 = 2308\) µs ≈ 2.3 ms——CPU 早就提交继续干别的去了，GPU 排队等流空出来才执行，这正是异步模型的意义（也是「数组顺序≠时间顺序」的根源）。脚本若找不到 `id`，检查是否误把 `fwdbwd` 也当成了 `ac2g`（此处已用 `cat` 过滤）。

#### 4.2.5 小练习与答案

**练习 1**：`s`/`f` 事件为什么需要 `bp:"e"` 这个字段？

**参考答案**：`bp` 指定箭头端点绑定到目标轨道的哪个位置，`"e"`（enclosing）表示绑定到该时刻**所在的外层切片末尾**，避免箭头指向轨道空白处；Chrome/Perfetto 依此决定箭头的视觉落点。对分析脚本而言它无信息量，可忽略。

**练习 2**：如何只用 `ph` 一个字段把 14240 条事件分成「占时间的」和「不占时间的」？

**参考答案**：`ph=="X"` 的占时间（有色块、有 `dur`），train.json 中即 4387+3853+963+42+8+1 = 9254 条；`ph` 为 `M`/`s`/`f`/`i` 的不占时间（标签、箭头、戳记），共 4986 条。做时间占比统计时必须先按 `ph`（或等价地按 `cat`）过滤，否则会重复计数。

**练习 3**：为什么 `ac2g`（4876 条）比 `kernel + cuda_runtime`（963 + 3853 = 4816 条）还多？

**参考答案**：箭头不止画在「API → GPU 活动」这一段：CPU 侧每个 CUDA API 调用自身也会被 `f`（`bp:"e"`）箭头包住（见 [train.json:L31151-L31162](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31151-L31162) 中 `cudaEventQuery` 前后的 `s`/`f`)，GPU 侧每个内核/拷贝后又各有一条 `f`。多条箭头服务于同一次调用，所以总数略多于活动数。（精确的 1:1 公式无需记住，记住「箭头数 ≥ 活动数」即可。）

### 4.3 cat：事件类别速查表与全量统计

#### 4.3.1 概念说明

`cat` 是事件的类别标签，回答「这件事发生在哪一层」。PyTorch Profiler（底层是 Kineto/CUPTI）把一次训练的活动分成几个层次，train.json 中实际出现的类别全表如下（计数经文本搜索核实，方法见 4.3.4）：

| `cat` | 层次 | 出现在哪条轨道 | train.json 计数 |
|---|---|---|---|
| `cpu_op` | CPU 上的框架算子（`aten::`、`autograd::` 等） | pid 1166 的两个线程 | 4387 |
| `cuda_runtime` | CPU 调用的 CUDA 运行时 API | pid 1166 主线程（tid 1166） | 3853 |
| `kernel` | GPU 上执行的计算内核 | GPU 0 的 stream 7/23/27 | 963 |
| `gpu_memcpy` | GPU 拷贝引擎活动（`Memcpy DtoD` 等） | GPU 0 的 stream | 8 |
| `user_annotation` | 用户/框架打的注解区间（`ProfilerStep#1`、`1F1B`…） | pid 1166 主线程 | 42 |
| `ac2g` | CPU→GPU 关联箭头 | 两侧都有 | 4876 |
| `fwdbwd` | 前向↔反向绑定箭头 | CPU tid 2571 | 68 |
| `Trace` | 采集窗口 span（`PyTorch Profiler (0)`） | `pid:"Spans"` | 1 |
| （无 `cat`） | `ph:"M"` 元数据 40 条 + `ph:"i"` 即时事件 2 条 | — | 42 |

合计 \(4387+3853+963+8+42+4876+68+1+42 = 14240\)，与 u1-l3 的事件总数吻合。

#### 4.3.2 核心流程

类别之间不是并列，而是一条**派生链**：

```text
cpu_op（CPU 算子，如 aten::ones）
   └─ 触发 cuda_runtime（CPU 调 cudaLaunchKernel / cudaMemcpyAsync）
        └─ 提交到 GPU stream → kernel / gpu_memcpy（GPU 侧实际执行）
ac2g/fwdbwd 流事件在上述节点之间画箭头（不占时间）
user_annotation 站在 cpu_op 之上做人工分段（ProfilerStep#1 ⊃ 1F1B ⊃ …）
ph:"M" 元数据给所有轨道起名字
```

两个按 `cat` 细分的有用统计（后续单元反复使用）：

- **kernel 按 stream 分**：stream 7（计算主流）907 个、stream 23 16 个、stream 27（通信流，DeepEP 内核所在）40 个——这就是 u1-l2 说「stream 7 占九成」的准确数字，也是 u2 单元区分「计算流/通信流」的基础。
- **cuda_runtime 按 API 分**：`cudaEventQuery` 2241 次（事件轮询，最频繁）、`cudaLaunchKernel` 727 次、`cudaMemcpyAsync` 8 次，其余 877 次是 `cudaLaunchKernelExC`、`cudaStreamWaitEvent`、`cudaFuncSetAttribute`、`cudaMemsetAsync` 等。

#### 4.3.3 源码精读

每类一个已核对的真实样例：

- `cpu_op`：[train.json:L72-L78](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L72-L78) —— `args` 只有 `External id` / `Ev Idx`（反向算子额外带 `Fwd thread id` / `Sequence number`）。
- `cuda_runtime`：[train.json:L31267-L31274](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31267-L31274) —— `args` = `External id` + `cbid`（API 编号）+ `correlation`。
- `kernel`：[train.json:L31247-L31262](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31247-L31262) —— `args` 最丰富：`stream`、`correlation`、`registers per thread`、`shared memory`、`blocks per SM`、`warps per SM`、`grid`、`block`、`est. achieved occupancy %`。交叉验证一下：\(grid[0]/numSms = 4096/132 \approx 31.03\)，正是 `blocks per SM: 31.03`——这个字段直接用上了 u1-l3 的 `deviceProperties`。
- `gpu_memcpy`：[train.json:L32887-L32896](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L32887-L32896) —— `Memcpy DtoD (Device -> Device)`，`args` 含 `bytes: 131072`（128 KiB）与实测带宽 `memory bandwidth (GB/s): 78.77`；它与 [train.json:L32901-L32908](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L32901-L32908) 的 `cudaMemcpyAsync` 共享 `correlation: 45234`（4.2.4 已算过它 2.3 ms 的滞留）。
- `user_annotation`：[train.json:L14416-L14436](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14416-L14436) —— 42 条 = `ProfilerStep#1`（1）+ `1F1B`（1）+ 4 组完整的 DualPipe 注解（每组 10 条：`attn(F/B/W)`、`mlp(F/B/W)`、`dispatch(F/B)`、`combine(F/B)`），4 组对应一个 chunk 的 4 个 MoE 层，u2-l2 展开。
- `Trace`：[train.json:L97629-L97636](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97629-L97636) —— `PyTorch Profiler (0)`，`pid:"Spans"`、`tid:"PyTorch Profiler"`，`dur: 129749` µs 即采集窗口长度（u1-l3 已解读）。

#### 4.3.4 代码实践

**实践目标**：复现上面的全量类别统计（这也是本讲综合实践脚本的核心函数）。

**操作步骤**（示例代码）：

```python
# count_cats.py —— 示例代码
import json
from collections import Counter

with open("train.json", "r", encoding="utf-8") as f:
    evts = json.load(f)["traceEvents"]

cats = Counter(e.get("cat", "(无cat)") for e in evts)
for cat, n in cats.most_common():
    print(f"{cat:16} {n}")

# kernel 按 stream 细分
streams = Counter(e["args"]["stream"] for e in evts if e.get("cat") == "kernel")
print("kernel 所在 stream:", dict(streams))
```

**需要观察的现象**：计数是否与 4.3.1 表格一致？`(无cat)` 是多少、由哪些 `ph` 构成？stream 分布是否一边倒？

**预期结果**：降序为 `ac2g 4876 > cpu_op 4387 > cuda_runtime 3853 > kernel 963 > fwdbwd 68 > (无cat) 42 = user_annotation 42 > gpu_memcpy 8 > Trace 1`，合计 14240；`(无cat)` 42 = `ph:"M"` 40 条 + `ph:"i"` 2 条；kernel 分布 `{7: 907, 23: 16, 27: 40}`。（以上计数是本讲义作者用正则文本搜索逐一核实过的，运行不一致时优先怀疑文件被截断。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cudaEventQuery` 一个「查询」API 就有 2241 次，比 `cudaLaunchKernel`（727）还多三倍？

**参考答案**：训练循环里大量使用 CUDA event 做流同步/就绪轮询——反复问「这件事做完了吗」。查询是廉价的忙等，所以次数远超真正的内核启动。读轨迹时这类事件耗时长尾（每次仅几微秒），但提醒你 CPU 可能在自旋等待。

**练习 2**：想统计「GPU 到底在算什么」，应该按哪个 `cat` 聚合？为什么不能直接把所有 `X` 事件加起来？

**参考答案**：按 `cat=="kernel"`（必要时加 `gpu_memcpy`）聚合。因为 `X` 事件横跨 CPU/GPU 多层且互相嵌套（`cpu_op` 套 `cuda_runtime`、`ProfilerStep#1` 套一切），直接求和会把同一段时间重复计算多遍。

**练习 3**：`blocks per SM` 是 31.03，`warps per SM` 是 124.12，后者为什么恰好约是前者的 4 倍？

**参考答案**：该内核 `block = [128,1,1]`，每块 128 线程 = 4 个 warp（warpSize=32），所以每 SM 的 warp 数 = 每块 warp 数 × 每 SM 块数 = 4 × 31.03 ≈ 124.12。三个字段互相自洽，可用来检验你对 `args` 的理解。

### 4.4 元数据事件：把 pid/tid 变成 CPU、GPU 0 与 stream 7

#### 4.4.1 概念说明

`ph:"M"` 事件不记录任何活动，它们的职责是**给轨道起名字**。train.json 的 40 条 M 事件分五种 `name`：

| M 事件的 `name` | 作用 | `args` 携带 |
|---|---|---|
| `process_name` | 给 pid 起进程名 | `{"name": "python3"}` |
| `process_labels` | 给 pid 贴标签（查看器轨道标题） | `{"labels": "CPU"}` / `{"labels": "GPU 0"}` |
| `process_sort_index` | 进程轨道的排序权重 | `{"sort_index": 整数}` |
| `thread_name` | 给 (pid, tid) 起线程名 | `{"name": "stream 7 "}` 等 |
| `thread_sort_index` | 线程轨道的排序权重 | `{"sort_index": 整数}` |

结构规律（train.json 的实际布局）：

- **进程级**：9 个进程各 3 条（name + labels + sort_index）——pid 1166 标签 `CPU`，pid 0~7 标签 `GPU 0` ~ `GPU 7`；外加 `pid:"Spans"` 一条 sort_index。
- **线程级**：GPU 0 的三个 tid 命名为 `stream 7`、`stream 23`、`stream 27`（注意原文**带尾随空格** `"stream 7 "`，写脚本做字符串匹配时要么 strip、要么精确带空格）；CPU 的 tid 2571 命名为 `thread 2571 (python3)`（**重复出现了两次**，内容相同）、tid 1166 命名为 `thread 1166 (python3)`。
- sort_index 有明显编码规律：CPU=1166（就是 pid 本身），GPU \(n\) = \(16777216 + n\)（即 `0x1000000 + n`），`Spans` = 536870912（`0x20000000`）——只是排序权重，无业务含义。

把 4.3 的类别统计与线程命名合起来，还能读出 CPU 两个线程的分工：`cpu_op` 在 tid 1166 上有 2347 条（前向算子、`ProfilerStep#1` 等注解、全部 `cuda_runtime` 调用都在这条主线程），tid 2571 上有 2040 条（文件开头成片的 `autograd::engine::evaluate_function: …` 反向算子）。结合 Linux「主线程 TID = PID」的常识，可判断 **1166 是跑训练循环的主线程，2571 是 autograd 引擎的 backward 工作线程**。

#### 4.4.2 核心流程

用 M 事件建立映射表的标准流程：

```text
遍历 traceEvents，筛选 ph == "M"
  ├─ name == "process_labels" → proc_labels[pid] = args["labels"]      # "CPU"/"GPU 0"
  ├─ name == "process_name"   → proc_names[pid]  = args["name"]        # "python3"
  ├─ name == "thread_name"    → thread_names[(pid, tid)] = args["name"] # "stream 7 "
  └─ name == "*_sort_index"   → 排序用，分析可忽略
之后任何事件 e 的可读位置 = proc_labels[e.pid] + " / " + thread_names[(e.pid, e.tid)]
例：pid=0, tid=27 → "GPU 0 / stream 27"
```

#### 4.4.3 源码精读

CPU 进程的三连元数据：

[train.json:L97395-L97412](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97395-L97412) —— `process_name`（python3）、`process_labels`（**CPU**）、`process_sort_index`（1166）三条，全部 `pid 1166, tid 0`。

GPU 0 的三连：

[train.json:L97413-L97430](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97413-L97430) —— 同构三件套，`process_labels` 为 **GPU 0**、sort_index 16777216；GPU 1~7 逐块等距排列（如 GPU 1 在 [train.json:L97431-L97448](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97431-L97448)，GPU 7 的 labels 在 [train.json:L97545-L97550](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97545-L97550)，sort_index 依次 +1 直到 16777223）。

GPU 0 的三条线程命名：

[train.json:L97557-L97592](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97557-L97592) —— `thread_name` + `thread_sort_index` 成对出现三组：tid 7 → `"stream 7 "`、tid 23 → `"stream 23 "`、tid 27 → `"stream 27 "`（引号内末尾都有一个空格）。

CPU 侧的线程命名：

[train.json:L97593-L97604](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97593-L97604) 与 [train.json:L97605-L97616](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97605-L97616) —— tid 2571 → `"thread 2571 (python3)"`，完全相同的事件连写两遍；[train.json:L97617-L97628](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97617-L97628) —— tid 1166 → `"thread 1166 (python3)"`。

`pid` 还可以是**字符串**这条格式彩蛋：

[train.json:L97629-L97643](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97629-L97643) —— `cat:"Trace"` 的 `PyTorch Profiler (0)` 事件用 `pid:"Spans"`、`tid:"PyTorch Profiler"` 两个字符串开辟出一条独立于 CPU/GPU 的「时间标尺」轨道，随后一条 `process_sort_index`（536870912）给它排序。

#### 4.4.4 代码实践

**实践目标**：构建 pid→标签、(pid,tid)→线程名 两张映射表，还原查看器左侧的轨道树。

**操作步骤**（示例代码）：

```python
# build_metadata.py —— 示例代码
import json

with open("train.json", "r", encoding="utf-8") as f:
    evts = json.load(f)["traceEvents"]

proc_labels, thread_names = {}, {}
for e in evts:
    if e.get("ph") != "M":
        continue
    if e["name"] == "process_labels":
        proc_labels[e["pid"]] = e["args"]["labels"]
    elif e["name"] == "thread_name":
        thread_names[(e["pid"], e["tid"])] = e["args"]["name"].strip()

for pid in (1166, 0):
    print(f"pid={pid} -> {proc_labels[pid]}")
    for (p, t), n in sorted(thread_names.items()):
        if p == pid:
            print(f"    tid={t} -> {n!r}")
```

**需要观察的现象**：pid 1166 与 pid 0 的标签分别是什么？各挂了几个线程？若去掉 `.strip()`，打印出的 stream 名会有什么不同？

**预期结果**：`pid=1166 -> CPU`（线程 `tid=1166 -> 'thread 1166 (python3)'`、`tid=2571 -> 'thread 2571 (python3)'`，后者在原始数据中出现两次、字典去重后只剩一条）；`pid=0 -> GPU 0`（`tid=7 -> 'stream 7'`、`tid=23 -> 'stream 23'`、`tid=27 -> 'stream 27'`）。不加 `.strip()` 时 stream 名以 `'stream 7 '` 形式带尾随空格——用 `== "stream 7"` 判断会失败，这是一个真实踩坑点。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GPU 1~7 都有命名元数据，4.3 的统计里却查不到它们上面的任何内核？

**参考答案**：命名只是「画好了轨道」。轨迹是 rank 0 单进程视角（u1-l3 的 `distributedInfo.rank: 0`），本进程只使用本机 GPU 0，所以 GPU 1~7 轨道全程空白——元数据齐全与轨道有活动是两回事。

**练习 2**：`tid 2571` 的 `thread_name` 重复写了两遍，会不会造成问题？

**参考答案**：不会，后写覆盖先写且内容完全相同（分析脚本用字典天然去重）。它更像是导出器的一个小冗余，同时提醒我们「M 事件可能出现多次，以最后一条为准」是处理元数据的稳妥策略。

**练习 3**：假如没有 `process_labels`，你还能用什么线索把 pid 0 认成 GPU 0？

**参考答案**：可以看该 pid 上事件的 `cat`（只有 `kernel`/`gpu_memcpy`/GPU 侧 `ac2g`）、`args.device`（值恒为 0）、以及 tid 集合恰为 7/23/27 这几个 stream 编号；再结合 `deviceProperties` 的 `id` 字段交叉印证。元数据只是把这套推理的结果提前写好了而已。

## 5. 综合实践

把四个模块合成一份「事件字段体检报告」，也就是本讲规格中指定的任务：**统计每个 `cat` 的事件数量并降序输出；解析 `ph:"M"` 元数据，输出 pid 1166 与 pid 0 的进程标签及各 tid 的线程名**。

```python
#!/usr/bin/env python3
# explore_event_fields.py —— 示例代码：事件字段体检
# 用法: python3 explore_event_fields.py train.json
import json
import sys
from collections import Counter

path = sys.argv[1] if len(sys.argv) > 1 else "train.json"
with open(path, "r", encoding="utf-8") as f:
    evts = json.load(f)["traceEvents"]

# ── 第一部分：cat 全量统计（降序）────────────────────────
cats = Counter(e.get("cat", "(无cat)") for e in evts)
print(f"== {path} 共 {len(evts)} 条事件 ==")
print("| cat | 数量 |")
print("|---|---|")
for cat, n in cats.most_common():
    print(f"| {cat} | {n} |")

# ── 第二部分：解析 ph="M" 元数据 ────────────────────────
proc_labels, thread_names = {}, {}
for e in evts:
    if e.get("ph") == "M" and e["name"] == "process_labels":
        proc_labels[e["pid"]] = e["args"]["labels"]
    if e.get("ph") == "M" and e["name"] == "thread_name":
        thread_names[(e["pid"], e["tid"])] = e["args"]["name"].strip()

print(f"\npid=1166 标签: {proc_labels.get(1166)}")
print(f"pid=0    标签: {proc_labels.get(0)}")
for pid in (1166, 0):
    for (p, t), n in sorted(thread_names.items()):
        if p == pid:
            print(f"  pid={pid} tid={t} -> {n!r}")

# ── 第三部分：附加核对 ──────────────────────────────────
streams = Counter(e["args"]["stream"] for e in evts if e.get("cat") == "kernel")
print("\nkernel 按 stream:", dict(sorted(streams.items())))
tids = Counter(e["tid"] for e in evts if e.get("cat") == "cpu_op")
print("cpu_op 按 CPU 线程:", dict(sorted(tids.items())))
```

**任务**：对 train.json 运行，核对三组输出；然后把命令行参数换成 prefill.json、decode.json 再各跑一次（脚本对三份文件通用，decode 单行格式不影响 `json.load`），记录三份文件的 cat 榜单前三名是否一致。

**预期结果**（train.json 部分，数字已由作者文本搜索核实）：

| cat | 数量 |
|---|---|
| ac2g | 4876 |
| cpu_op | 4387 |
| cuda_runtime | 3853 |
| kernel | 963 |
| fwdbwd | 68 |
| (无cat) | 42 |
| user_annotation | 42 |
| gpu_memcpy | 8 |
| Trace | 1 |

元数据部分输出 `pid=1166 标签: CPU`、`pid=0 标签: GPU 0`，以及 4.4.4 列出的线程名；kernel 按 stream 为 `{7: 907, 23: 16, 27: 40}`，cpu_op 按线程为 `{1166: 2347, 2571: 2040}`。prefill/decode 的具体计数留给你运行填写（**待本地验证**）——可以预期榜单前三名仍是 ac2g / cpu_op / cuda_runtime（这三类在任何 PyTorch CUDA 轨迹里都是大头），但 prefill 的 kernel 数会远多于 train（85422 条总事件 vs 14240）。

## 6. 本讲小结

- 一条事件 = 八字段名片：`ph` 定类型、`cat` 定类别、`name` 定具体事、`pid`/`tid` 定轨道、`ts`/`dur` 定区间（\([ts, ts+dur]\)，微秒）、`args` 装各类细节。
- 本仓库轨迹只出现五种 `ph`：`X`（有色块的完成事件，可嵌套）、`M`（元数据）、`s`/`f`（流箭头，按 `id` 配对）、`i`（即时戳记）；做时间统计只用 `X`。
- train.json 的 cat 榜单：`ac2g` 4876、`cpu_op` 4387、`cuda_runtime` 3853、`kernel` 963、`fwdbwd` 68、无 cat 42、`user_annotation` 42、`gpu_memcpy` 8、`Trace` 1，合计 14240。
- 类别是派生链：CPU 算子（cpu_op）调用 CUDA API（cuda_runtime）→ GPU 上执行（kernel/gpu_memcpy）；`ac2g` 箭头把两端连起来，`user_annotation` 在 CPU 侧做人工分段。
- 元数据事件把数字变名字：`process_labels` 标出 CPU（pid 1166）与 GPU 0~7（pid 0~7），`thread_name` 标出 stream 7/23/27（原始名带尾随空格）与 CPU 的主线程 1166、autograd 工作线程 2571。
- kernel 集中在 GPU 0：stream 7（计算主流）907 个、stream 23 16 个、stream 27（通信流）40 个；GPU 1~7 只有命名没有活动（rank 0 视角）。

## 7. 下一步学习建议

字段层面已经通关，第二单元第一讲 **u2-l1《CPU 与 GPU 的关联：cuda_runtime、External id 与 ac2g》**会把本讲的两个线索正式接上：`args.correlation` 如何把 `cudaLaunchKernel` 与 GPU 内核一一配对、`External id` 如何再往上追到 `cpu_op` 算子，最终拼出「GPU 内核 ← CUDA API ← CPU 算子」的完整调用链。建议先把本讲的 `explore_event_fields.py` 在三份轨迹上各跑一遍并留存输出——u3-l1 的通用分析脚本将直接以它为雏形。阅读源码顺序上，可以顺便重看 [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg)：现在图中每条色块、每根箭头、每行轨道标题，你都应该能说出它对应的字段组合了。
