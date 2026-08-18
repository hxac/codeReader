# 用 Python 做轨迹定量分析

> 所属单元:u3 · 定量分析与二次实践(advanced)
> 前置讲义:u2-l1(CPU 与 GPU 的关联)。本讲会在其之上,把 u1/u2 里一条条「点查」沉淀成一个可复用的分析脚本。

## 1. 本讲目标

学完本讲,你应该能够:

1. 用 Python(仅标准库)加载任意一份 PyTorch Profiler 导出的 Chrome Trace JSON,并妥善处理 train/prefill(格式化多行)与 decode(单行压缩)两种排版。
2. 编写按 `cat`、按内核名、按 `pid`/`stream` 三个维度聚合的通用统计函数,并明白**为什么只有 `ph == "X"` 的事件才能计入时长**。
3. 输出 Top-N 耗时内核与各类别/内核族的时间占比,用数字(而不是截图直觉)回答「时间都花在哪了」。
4. 熟练完成微秒 → 毫秒换算与区间端点计算 \( [ts,\; ts + dur) \),为下一讲的重叠率分析打好基础。

本讲的产出物是一个脚本 `analyze_trace.py`(示例代码),它对三份轨迹都能运行,也是 u3-l2、u3-l3 两讲的地基。

## 2. 前置知识

本讲不需要新的分布式系统知识,但需要以下 Python 与前置讲义的概念:

- **`collections.defaultdict` 与 `Counter`**:聚合统计的两把瑞士军刀。`defaultdict(int)` 做「键不存在则从 0 开始累加」,`Counter` 做「按键计数」。本讲的分组求和用 `defaultdict` 最顺手。
- **`sorted` 的 `key` 参数**:事件数组在文件里**不按时间排序**(u1-l3 已确认),分析前必须 `sorted(events, key=lambda e: e["ts"])`;Top-N 则按总时长降序 `sorted(..., key=..., reverse=True)`。
- **单位与区间**:`ts` 与 `dur` 均为**微秒**(u1-l4)。一个事件占据的时间区间是左闭右开区间 \( [ts,\; ts + dur) \),端点差即时长。换算毫秒除以 1000。
- **`ph == "X"` 才有 `dur`**:`M`(元数据)、`s`/`f`(流箭头)、`i`(即时戳记)都没有时长,把它们加进时间统计会把 `KeyError` 或脏数据带进结果(u1-l4 的五种 ph 回顾)。
- **`cat` 的派生链**(u1-l4/u2-l1):`cpu_op`(CPU 算子)→ `cuda_runtime`/`cuda_driver`(CUDA API 调用)→ `kernel`/`gpu_memcpy`/`gpu_memset`(GPU 执行),另有 `ac2g`(CPU→GPU 箭头)与 `user_annotation`(框架打点)。
- **`args.stream`**:GPU 侧事件的 `args` 里有 `stream` 字段,且 kernel 事件的 `tid` 就是 stream 号(u1-l4)。按流分组是区分「计算流 / 通信流」的正规手段(u2-l4 的教训:按 `args.stream` 字段分组,而不是按名称子串猜测)。

## 3. 本讲源码地图

本讲的分析对象是仓库的全部三份数据文件(它们就是「源码」):

| 文件 | 体积 / 行数 | 排版 | traceEvents 总数 | world_size | 本讲关键锚点 |
| --- | --- | --- | --- | --- | --- |
| `train.json` | 3.1 MB / 97653 行 | 格式化多行 | 14240 | 64 | 见下表 |
| `prefill.json` | 17.5 MB / 570649 行 | 格式化多行 | 85422 | 32 | 见下表 |
| `decode.json` | 4.7 MB / **1 行** | 单行压缩 | 19417 | 128 | 见下表 |

各文件中本讲会反复引用的位置:

| 位置 | 内容 |
| --- | --- |
| [train.json:L70-L71](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70-L71) | `distributedInfo`(world_size=64)与 `traceEvents` 数组起点 |
| [train.json:L73-L78](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L73-L78) | 第一条完整 `cpu_op` 事件(含 `ts`/`dur`/`args`) |
| [train.json:L14424-L14429](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14424-L14429) | `1F1B` 用户注解,`dur` = 112168µs |
| [train.json:L31324-L31338](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324-L31338) | DeepEP `internode::dispatch` 内核完整事件(带 `args.stream`/`correlation`/`grid`) |
| [train.json:L97396-L97430](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97396-L97430) | 文件尾部的 `ph:"M"` 元数据(`CPU` / `GPU 0` 标签) |
| [train.json:L97653](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97653) | `traceName`,记录原始实验配置 |
| [prefill.json:L14](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L14) | `distributedInfo`(world_size=32) |
| [prefill.json:L87](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L87) | `nccl:all_reduce` 用户注解(prefill 仅有的 6 条注解之一) |
| [prefill.json:L175934](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L175934) | prefill 首个 kernel 事件:`internode::dispatch`,`tid` = 16(通信流) |
| [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) | 整份文件只有这一行;所有事件内联排列,行内定位只能靠 JSON 解析 |

> 提醒:以上行号均对应当前 HEAD(`4496024`),与 u1/u2 各讲使用的永久链接一致。

## 4. 核心概念与源码讲解

### 4.1 JSON 加载与大文件处理

#### 4.1.1 概念说明

三份轨迹虽然内容同构(四个顶层字段,见 u1-l3),但**排版完全不同**:

- `train.json`、`prefill.json` 是「展开」的:每个事件占 6~15 行,字段名和值分行书写,人眼可以直接阅读;
- `decode.json` 是「压扁」的:整份 4.7 MB 挤在**一个物理行**里,编辑器打开会卡,`wc -l` 报 0(没有换行符),任何「按行统计」的文本工具都会失真。

这带来两个结论:

1. **分析必须走 JSON 解析器**。`json.load` 一次性把整个文件解析成 Python 对象,不关心排版差异——对这三份文件(最大 17.5 MB)完全够用,内存峰值在普通笔记本电脑可承受范围内(具体峰值待本地验证)。
2. **写脚本时不要对排版做任何假设**。不要用正则去「 grep 出第 N 个事件」,要用 `data["traceEvents"]` 这个列表。

#### 4.1.2 核心流程

```
打开文件(json.load)
    ↓
取出 data["traceEvents"](列表,长度 = 事件总数)
    ↓
按 ts 升序排序(原始数组按记录顺序排列,不是时间顺序 —— u1-l3 的教训)
    ↓
(可选)用 ph=="M" 的元数据事件建立 pid/tid → 可读名称 的映射
    ↓
进入 4.2 的聚合统计
```

#### 4.1.3 源码精读

**(1) 顶层字段的位置。** train.json 的 `distributedInfo` 与 `traceEvents` 起点在 [train.json:L70-L71](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70-L71):

```json
"distributedInfo": {"backend": "nccl", "rank": 0, "world_size": 64},
"traceEvents": [
```

这一行同时告诉我们:采集进程是 rank 0、NCCL 后端、EP64。prefill 的对应行在 [prefill.json:L14](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L14)(world_size=32);decode 的同一字段内联在 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) 中,内容为 `{"backend": "nccl", "rank": 0, "world_size": 128}`。三行对照,就是三份轨迹最快的「身份验明」。

**(2) 一条完整事件的形状。** 文件的第一条事件([train.json:L73-L78](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L73-L78)):

```json
{
  "ph": "X", "cat": "cpu_op", "name": "autograd::engine::evaluate_function: BackwardWithAuxiliaryLossBackward", "pid": 1166, "tid": 2571,
  "ts": 1740461679479712, "dur": 85,
  "args": {
    "External id": 5633,"Ev Idx": 0, "Fwd thread id": 1, "Sequence number": 20703
  }
}
```

注意 `ts` 是 16 位数字——Unix 纪元微秒(u1-l4)。任何「时间差」都要用微秒相减后再换算单位。

**(3) decode.json 的单行排版。** 打开 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1),开头是:

```
{"schemaVersion": 1, "deviceProperties": [{"id": 0, "name": "NVIDIA H800", ...
```

事件以 `{"ph": "X", "cat": "kernel", "name": "...", "pid": 0, "tid": 7, "ts": ..., "dur": ..., "args": {...}}` 的形式一个接一个内联。**作者在写本讲时踩过一个坑,值得你绕开:用文本工具统计 `decode.json` 里 `"cat": "kernel"` 的出现次数,得到的答案是 1——因为整份文件只有一行,「匹配行数」永远 ≤ 1。用 JSON 解析后实际数出的是 3647 个 kernel 事件。** 这就是「必须走解析器」的实证。

**(4) 文件尾部的元数据与 traceName。** [train.json:L97396-L97430](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97396-L97430) 是 `ph:"M"` 事件:它们把 `pid: 1166` 标记为 `CPU`、`pid: 0` 标记为 `GPU 0`。这些事件**没有 `cat` 也没有 `dur`**,统计时要自然地被「时长聚合」跳过。文件最后一行 [train.json:L97653](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97653) 的 `traceName` 值为 `traces/prof_ep64_tp1_varsm_hidden7168_seqlen4096.json`,一眼可读出「EP64 + TP1 + 7168 隐藏维 + 4096 序列长」的实验配置。

#### 4.1.4 代码实践

**实践一:三份轨迹的「验明正身」脚本**

1. **实践目标**:写一个 `load_and_report.py`,输出每份轨迹的 `schemaVersion`、`world_size`、`traceEvents` 数量,验证「同构」的说法。
2. **操作步骤**(示例代码):

   ```python
   #!/usr/bin/env python3
   # 示例代码:加载轨迹并打印基本信息
   import json

   for path in ["train.json", "prefill.json", "decode.json"]:
       with open(path, encoding="utf-8") as f:
           data = json.load(f)
       ev = data["traceEvents"]
       print(f"{path:14s} schemaVersion={data.get('schemaVersion')} "
             f"world_size={data['distributedInfo']['world_size']:>3d} "
             f"events={len(ev)}")
   ```

3. **需要观察的现象**:三行输出依次打印;`decode.json` 虽是单行文件,加载并不报错。
4. **预期结果**(作者已用文本统计工具逐项核实):

   ```text
   train.json     schemaVersion=1 world_size= 64 events=14240
   prefill.json   schemaVersion=1 world_size= 32 events=85422
   decode.json    schemaVersion=1 world_size=128 events=19417
   ```

5. 若你想进一步验证事件数组无序:打印 `ev[0]["ts"]` 与 `min(e["ts"] for e in ev)`,两者通常不相等(待本地验证)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `wc -l decode.json` 输出 0,而文件明明有 4.7 MB?
**答案**:`wc -l` 统计的是换行符个数;decode.json 是单行压缩 JSON,整个文件没有任何换行符,所以计 0。这也是文本工具不适合分析它的原因。

**练习 2**:如果轨迹文件大到几百 MB、`json.load` 内存吃紧,有什么替代方案?
**答案**:改用流式(增量)解析库,如第三方的 `ijson`,它按迭代器逐个产出 `traceEvents` 数组里的元素,内存占用与文件大小解耦。本仓库三份文件最大 17.5 MB,标准库足够。

**练习 3**:统计事件总数时,用 `len(data["traceEvents"])` 和「grep `"ph":` 出现次数」哪个可靠?为什么?
**答案**:前者可靠。后者依赖排版:格式化文件里每条事件恰好一行 `ph`,但元数据事件的字段顺序不同(如 `process_name` 行里 `ph` 在 `name` 之后仍会出现),而单行文件里 grep 计数直接失真。结构化数据要用结构化解析。

### 4.2 按类别 / 内核名 / 流的聚合统计

#### 4.2.1 概念说明

「聚合」就是把上万条事件沿某个维度**分组后分别求(次数, 总时长)**。三个维度回答三个不同的问题:

| 分组键 | 回答的问题 | 取值来源 |
| --- | --- | --- |
| `cat` | 时间花在哪一层(CPU 算子?CUDA API?GPU 内核?) | 事件顶层的 `cat` |
| 内核名 | 时间花在哪个函数上 | `cat=="kernel"` 事件的 `name` |
| `pid` / `stream` | 时间花在哪个设备 / 哪条队列上 | `pid` + `args.stream`(kernel 事件的 `tid` 即 stream) |

两个必须遵守的口径:

- **时长只累计 `ph == "X"` 的事件**。`M`/`s`/`f`/`i` 没有 `dur`,混入会污染统计。
- **CPU 侧与 GPU 侧时长不可相加比较**。`cpu_op` 时长是 CPU 墙钟(含异步提交后的等待,u2-l2 的 dispatch(F) 20ms 之辨),`kernel` 时长才是 GPU 真执行。占比计算只应在同一 `cat` 内部做。

#### 4.2.2 核心流程

```
for e in events:
    if e["ph"] != "X": continue        # 只有完成事件有时长
    key = (e["cat"], e["name"], e["args"].get("stream"))  # 按需选一维
    agg[key]["count"] += 1
    agg[key]["dur_us"] += e["dur"]
排序输出:
    按 count 降序 → 「数量榜」(结构指纹,u2-l4 用它反推出 61 层)
    按 dur_us 降序 → 「耗时榜」(下一节 Top-N)
```

占比的数学定义(以内核族为例):

\[
\text{share}(\text{family}) = \frac{\displaystyle\sum_{e \in \text{family}} e.\text{dur}}{\displaystyle\sum_{e \in \text{kernel}} e.\text{dur}} \times 100\%
\]

#### 4.2.3 源码精读

**(1) `args` 里藏着分组所需的全部字段。** 看 DeepEP 派发内核的完整事件 [train.json:L31324-L31338](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324-L31338):

```json
{
  "ph": "X", "cat": "kernel", "name": "void dpsk::ep::internode::dispatch<false, 8, true, 8>(...)", "pid": 0, "tid": 27,
  "ts": 1740461679472450, "dur": 3320,
  "args": {
    "External id": 44508,
    "queued": 0, "device": 0, "context": 1,
    "stream": 27, "correlation": 44508,
    "registers per thread": 110,
    "shared memory": 332,
    "blocks per SM": 0.15151516,
    "warps per SM": 2.4242425,
    "grid": [20, 1, 1],
    "block": [512, 1, 1],
    "est. achieved occupancy %": 4
  }
}
```

这一条事件就给出了:分组键 `cat="kernel"`、`args.stream=27`(通信流)、名称(DeepEP dispatch);时长 `dur=3320`µs≈3.32ms;以及 `correlation=44508`——u2-l1 讲过的 CPU↔GPU 外键,紧随其后的 [train.json:L31339-L31341](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31339-L31341) 就是一条 `ph:"f"`、`id: 44508` 的 ac2g 箭头事件,与它配对。`ph:"f"` 没有 `dur`,再次印证 4.2.1 的口径。

**(2) 按 cat 聚类的实测结果。** 作者用文本统计对三份文件做了逐类计数(下表「count」列全部实测核实;「total_ms」列为脚本应算出的值,其中多数待本地验证):

| cat | train 数量 | prefill 数量 | decode 数量 | 含义 |
| --- | --- | --- | --- | --- |
| ac2g | 4876 | 30125 | 12525 | CPU→GPU 箭头(无 dur) |
| cpu_op | 4387 | 25125 | 2674 | CPU 算子 |
| cuda_runtime | 3853 | 25606 | 452 | CUDA 运行时 API |
| cuda_driver | 40 | 367 | 7 | CUDA 驱动 API |
| kernel | 963 | 4111 | 3647 | GPU 内核 |
| gpu_memcpy | 8 | 37 | 66 | 显存拷贝 |
| gpu_memset | 12 | 4 | 3 | 显存清零 |
| user_annotation | 42 | 6 | 3 | 框架/用户打点 |
| fwdbwd | 16 | — | — | 前反向标记(train 特有) |

(另有 40 条 `ph:"M"` 元数据事件无 `cat`,以及个位数长尾事件,所以各列相加略小于事件总数。)

注意 decode 的 `cpu_op` 只有 2674 条、`cuda_runtime` 只有 452 条——**CUDA Graph 回放把整条前向打包成一次 `cudaGraphLaunch`,CPU 侧记录因此骤减**(u2-l5 已确认 3437 个内核共享 correlation 7077;本讲实测 `"correlation": 7077` 在全文件出现 3438 次 = GPU 侧 3437 条 + CPU 侧 1 条)。聚合表本身就是「采集方式」的指纹。

**(3) 按 stream 聚类的实测结果**(kernel 事件按 `args.stream` 分组,作者实测):

| 文件 | stream → kernel 数 | 解读 |
| --- | --- | --- |
| train.json | 7 → 907;27 → 40;23 → 16 | stream 7 计算主流;27 上是 DeepEP 通信(u2-l3 的 36 个通信内核再加少量杂项);23 零星 |
| prefill.json | 7 → 3525;16 → 580;13 → 6 | 16 是 DeepEP 通信流(580 个,u2-l4);13 是 NCCL |
| decode.json | 待本地验证(已知 ll 内核 `tid` = 7,与计算同流) | 低延迟内核不占独立流(u2-l5) |

**(4) 元数据把 pid 翻译成轨道名。** [train.json:L97396-L97430](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97396-L97430):`process_labels` 事件把 `pid 1166 → "CPU"`、`pid 0 → "GPU 0"`。做「按 pid 汇总」时,先扫一遍 `ph:"M"` 事件建 `{(pid): label}` 字典,报表可读性大增;注意 stream 名字符串带尾随空格,匹配时要 `strip()`(u1-l4 的提醒)。

**(5) user_annotation 是现成的「粗粒度汇总」。** [train.json:L14424-L14429](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14424-L14429) 的 `1F1B` 注解 `dur=112168`µs≈112.2ms,就是 u2-l2 反复使用的训练窗口长度;prefill 里仅有的 6 条注解全是 `nccl:all_reduce`([prefill.json:L87](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L87) 起的第一条),decode 只有 3 条——注解密度本身就区分了三种采集方式。

#### 4.2.4 代码实践

**实践二:cat 维度聚合**

1. **实践目标**:产出三份轨迹的「cat × 数量 × 总时长」表。
2. **操作步骤**(示例代码):

   ```python
   # 示例代码:按 cat 聚合数量与总时长
   import json
   from collections import defaultdict

   def stats_by_cat(path):
       with open(path, encoding="utf-8") as f:
           events = json.load(f)["traceEvents"]
       cnt, dur = defaultdict(int), defaultdict(int)
       for e in events:
           c = e.get("cat", "<无cat>")
           cnt[c] += 1
           if e.get("ph") == "X":          # 只有完成事件有时长
               dur[c] += e.get("dur", 0)
       for c in sorted(cnt, key=cnt.get, reverse=True):
           print(f"{c:<16s} count={cnt[c]:>6d}  total={dur[c]/1000:>10.3f} ms")
   ```

3. **需要观察的现象**:train.json 输出 9 行左右;`ac2g` 的 total 恒为 0;`user_annotation` 的总时长远大于 42 × 平均(注解互相嵌套,直接相加是「重复计账」)。
4. **预期结果**:count 列与 4.2.3 (2) 的表格逐行一致(已实测);total 列待本地验证。一个已知锚点:train 的 `kernel` 总时长约为 244 ms(由 4.3 节的实测「DeepEP 家族 113.44ms = 46.5%」反推,待本地验证)。
5. 若 `dur` 列出现 `KeyError`,说明你漏了 `ph == "X"` 过滤。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `sorted(events, key=lambda e: e.get("ts", 0))` 要给 `ts` 一个默认值 0?
**答案**:部分事件(如某些元数据或流事件)可能缺 `ts` 字段,直接 `e["ts"]` 会抛 `KeyError`;给默认值让排序对这些事件有确定行为(排到最前面)。

**练习 2**:统计「每个 pid 的 kernel 总时长」时,train.json 里 GPU 1~7 的结果是多少?为什么?
**答案**:0。u1-l2 已确认 963 个内核全部落在 GPU 0(pid 0),GPU 1~7 只是版式上的空轨道——采集视角是 world_size 64 中的 rank 0。

**练习 3**:如何用聚合结果快速区分 prefill.json 与 decode.json(不看 world_size)?
**答案**:看 `kernel` 与 `cpu_op` 的比例:prefill 是 4111 kernel / 25125 cpu_op(CPU 逐算子记录),decode 是 3647 kernel / 2674 cpu_op(CUDA Graph 回放,CPU 记录被折叠)。也可以看注解:prefill 有 6 条 `nccl:all_reduce`,decode 只有 3 条注解。

### 4.3 Top-N 与占比计算

#### 4.3.1 概念说明

Top-N 是「耗时榜」:把 `cat=="kernel"` 的事件按 `name` 聚合后,按总时长降序取前 N 名。它背后是性能分析中的帕累托直觉——**少数内核家族通常占据大部分 GPU 时间**,优化要先看头部。

两个工程细节:

- **名称归一化**:C++ 模板内核名可能长达数百字符(FlashAttnBwd 的名字带整套 cute::tuple 参数),不同 shape 的同名内核还带不同模板实参。直接按全名聚合会把一个家族拆成很多行;按「截断前缀」或「去掉尖括号内容」聚合才能看出家族占比。截断是损失最小的简易做法。
- **占比的分母**:用同一 `cat`(kernel)的总时长,而不是墙钟。多流并行时各流时长之和可以超过墙钟(train 的 stream 7 与 27 并行,u2-l2),这是「占用时间」而非「流逝时间」。

#### 4.3.2 核心流程

```
agg = {name → (n, dur_us, streams)}
for e in kernel 事件:
    key = normalize(e["name"])
    agg[key].n        += 1
    agg[key].dur_us   += e["dur"]
    agg[key].streams.add(e["args"]["stream"])
total = Σ dur_us
Top-N = sorted(agg, by dur_us desc)[:N]
每行输出: 名称 | 次数 | 总时长(ms) | 占比 | 所在流
```

#### 4.3.3 源码精读(实测数据)

**(1) train.json 的大内核构成。** 作者对 963 个 kernel 的 `dur` 做了直方图实测:**时长 ≥ 2ms 的内核共 31 个**,其构成为:

| 家族 | 个数 | 实测时长(µs) | 说明 |
| --- | --- | --- | --- |
| DeepEP(`dpsk::ep::internode::` dispatch/combine/notify) | 23 | 2186 ~ 6549 | 通信内核,最大 6.55ms(与 u2-l3 的 6.5ms combine 窗口吻合) |
| `cutlass::device_kernel<flash::FlashAttnBwd<...>>` | 4 | 3583 / 3809 / 3860 / 3863 | 注意力反向内核(attn 的 B/W) |
| `cutlass::device_kernel<dpsk::grouped_gemm::...GemmKernel>` | 4 | 2025 / 2045 / 2126 / 2159 | MoE 分组 FP8 GEMM(mlp 的 W) |

进一步把全部 36 个 DeepEP 内核的时长求和(作者对直方图逐项相加):**113 440 µs ≈ 113.44 ms**。结合 u2-l3 确立的「DeepEP 占 GPU kernel 总时长 46.5%」,可反推:

\[
T_{\text{kernel}} \approx \frac{113.44\ \text{ms}}{0.465} \approx 244\ \text{ms}
\]

这就是 4.2.4 实践里「train 的 kernel 总时长约 244ms」的来历(推算值,待本地验证)。Top 榜的结论清晰:**训练轨迹的 GPU 时间头部队伍 = DeepEP 通信 + 注意力反向 + 分组 GEMM**,正是 DualPipe 想用重叠隐藏的三类大块。

**(2) decode.json 的 dispatch_ll 双峰直方图。** 对 235 个 `dpsk::ep::internode::dispatch_ll<true, 3, 10, 7168>` 内核的时长做直方图(作者实测):

| 时长段 | 个数 | 对应 u2-l5 的解释 |
| --- | --- | --- |
| 17 / 18 / 19 µs | 76 + 37 + 4 = 117 | 「发送段」:RDMA 消息发完即释放 SM |
| 58 ~ 86 µs | 114 | 「等待段」:内核挂起等对端 |
| 37 / 134 / 202 / 1603 µs | 各 1(共 4) | 离群点(边界/异常槽) |

合计 235 个、总时长 11 921 µs ≈ 11.92 ms。这组数字把 u2-l5 的「三段式」论断变成了可复现的统计量:**同一个内核名的 dur 直方图就是调度行为的指纹**。`combine_ll` 同样是 235 个(实测计数),其时长分布待本地验证。

**(3) 名字归一化的必要性。** prefill 首个内核 [prefill.json:L175934](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L175934) 的名字是 `void dpsk::ep::internode::dispatch<false, 4, false, 4>(int4*, float*, ...)`——模板参数 `<false, 4, false, 4>` 与 train 的 `<false, 8, true, 8>` 不同(不同 world_size/通道配置编译出的变体)。若按全名聚合,「DeepEP dispatch」会被拆成多行;截断到模板尖括号前(如 `void dpsk::ep::internode::dispatch`)才能得到家族级 Top-N。u2-l4 已用「次数指纹」(842 次 fp8_gemm、122 次 compute_attn_ws、116 次路由链)展示了同样的家族视角。

#### 4.3.4 代码实践

**实践三:Top-20 内核榜**

1. **实践目标**:对 train.json 输出按名称聚合的 Top-20 耗时内核,含次数、总时长、占比、所在流。
2. **操作步骤**:把 4.3.2 的伪代码落成函数(完整脚本见第 5 节),运行:

   ```bash
   python3 analyze_trace.py train.json 20
   ```

3. **需要观察的现象**:榜首应被 `dpsk::ep::internode::` 家族占据;榜单里能看到 `FlashAttnBwd` 与 `grouped_gemm` 的截断名;`stream` 列大部分是 7,通信行是 27。
4. **预期结果**:与 4.3.3 (1) 的实测一致——DeepEP 家族合计 36 次、113.44ms、占比约 46.5%;FlashAttnBwd、grouped_gemm 紧随其后。完整 20 行数值待本地验证。
5. **常见坑**:若占比相加明显超过 100%,检查你是否把 `cpu_op` 时长也加进了分母;若通信行消失,检查名称截断是否把 `<...>` 后的签名残片当成了不同名字。

#### 4.3.5 小练习与答案

**练习 1**:`dur` 直方图除了验证 dispatch_ll 的双峰,还能用来做什么?
**答案**:识别异常槽(如 1603µs 的离群点值得单独查看其 `ts` 上下文);对比不同配置下同类内核的形态(train 的 dispatch 毫秒级 vs decode 的 dispatch_ll 微秒级,正是 normal 与 low-latency 两代内核的差异,u2-l5)。

**练习 2**:为什么「按总时长排序」而不是「按次数排序」?
**答案**:两者回答不同问题。次数榜是结构指纹(u2-l4 用 122=2×61 反推层数);耗时榜才是优化的优先级依据。decode 里 3647 个内核中 dispatch_ll 只占 235 个(6.4%),却占 19.0% 时长(u2-l5),只看次数会完全误判。

**练习 3**:同一内核名的多个事件落在不同 stream(train 的 stream 27 上有 40 个内核,其中 36 个是 DeepEP),聚合时怎么呈现?
**答案**:给每个名称分组维护一个 `streams` 集合,输出时打印排序后的流列表;若需要更细的口径,把分组键改成 `(name, stream)` 二元组。

## 5. 综合实践

把三个模块合起来,写成一个对任意 PyTorch Chrome Trace 都能用的命令行工具。**以下为示例代码**(仓库中不存在,需自行创建;建议放在仓库外的练习目录,或本教程目录下):

```python
#!/usr/bin/env python3
# 示例代码:analyze_trace.py —— 通用 Chrome Trace 定量分析工具
# 用法: python3 analyze_trace.py <trace.json> [top_n]
import json
import sys
from collections import defaultdict


def load_trace(path):
    """加载轨迹:返回(元信息, 按 ts 升序的事件列表)。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    events = sorted(data["traceEvents"], key=lambda e: e.get("ts", 0))
    meta = {
        "schemaVersion": data.get("schemaVersion"),
        "world_size": data.get("distributedInfo", {}).get("world_size"),
        "n_events": len(events),
    }
    return meta, events


def stats_by_cat(events):
    """按 cat 聚合:数量与总时长(仅 ph=='X' 计入时长)。"""
    cnt, dur = defaultdict(int), defaultdict(int)
    for e in events:
        c = e.get("cat", "<无cat>")
        cnt[c] += 1
        if e.get("ph") == "X":
            dur[c] += e.get("dur", 0)
    return cnt, dur


def normalize_name(name, limit=72):
    """把带超长模板参数的内核名压成可读前缀,便于家族级聚合。"""
    return name if len(name) <= limit else name[:limit] + "..."


def top_kernels(events, top_n=20):
    """cat=='kernel' 的事件按名称聚合,返回 (Top-N 列表, kernel 总时长µs)。"""
    agg = defaultdict(lambda: {"n": 0, "dur_us": 0, "streams": set()})
    for e in events:
        if e.get("cat") != "kernel" or e.get("ph") != "X":
            continue
        a = agg[normalize_name(e["name"])]
        a["n"] += 1
        a["dur_us"] += e.get("dur", 0)
        s = e.get("args", {}).get("stream")
        if s is not None:
            a["streams"].add(s)
    total = sum(a["dur_us"] for a in agg.values())
    ranked = sorted(agg.items(), key=lambda kv: kv[1]["dur_us"], reverse=True)
    return ranked[:top_n], total


def main(path, top_n=20):
    meta, events = load_trace(path)
    print(f"== {path} ==")
    print(f"schemaVersion={meta['schemaVersion']} "
          f"world_size={meta['world_size']} events={meta['n_events']}")

    cnt, dur = stats_by_cat(events)
    print(f"\n[按 cat]")
    for c in sorted(cnt, key=cnt.get, reverse=True):
        print(f"  {c:<16s} count={cnt[c]:>6d} total={dur[c] / 1000:>10.3f} ms")

    ranked, total = top_kernels(events, top_n)
    print(f"\n[kernel 总时长 ≈ {total / 1000:.1f} ms] Top-{len(ranked)}:")
    for name, a in ranked:
        share = a["dur_us"] / total * 100 if total else 0.0
        print(f"  {a['dur_us'] / 1000:>8.2f} ms {a['n']:>4d} 次 "
              f"stream={sorted(a['streams'])} {share:5.1f}%  {name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: python3 analyze_trace.py <trace.json> [top_n]")
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 20)
```

**操作步骤**:

1. 在仓库根目录创建 `analyze_trace.py`(示例代码),依次运行:

   ```bash
   python3 analyze_trace.py train.json   20
   python3 analyze_trace.py prefill.json 20
   python3 analyze_trace.py decode.json  20
   ```

2. 用重定向把三份结果各自存成文本,例如 `python3 analyze_trace.py train.json 20 > train_report.txt`。
3. 在三份报告里各找一行「最意外的结果」,写一两句注释。

**需要观察的现象与预期结果**(作者实测锚点,完整数值待本地验证):

- 三份报告的 `events=` 行分别为 14240 / 85422 / 19417,`world_size=` 分别为 64 / 32 / 128。
- train 榜首是 `dpsk::ep::internode::` 家族(36 次、113.44ms、约 46.5%、stream 27);`FlashAttnBwd`、`grouped_gemm` 出现在前列(stream 7)。
- prefill 榜首同样由 DeepEP 通信家族与 DeepGEMM/注意力内核竞争(u2-l4:通信内核 580 个、占 48%,在 stream 16)。
- decode 榜首是 `flash_fwd_splitkv_mla` 家族(各 120 次,合计 34.9%,u2-l6)与 `dispatch_ll/combine_ll`(各 235 次,合计 19.0%,u2-l5);且 decode 报告的 `cpu_op`/`cuda_runtime` 行数量骤减(CUDA Graph 效应)。
- `dispatch_ll` 若做时长直方图(把 `top_kernels` 改成收集 `dur` 列表),应看到 17~19µs 与 58~86µs 两个峰(4.3.3 实测)。

**验收标准**:脚本对三份轨迹都不崩溃、不依赖第三方库;count 类数字与第 4 节表格一致;你能用一句话向同伴解释每份报告的「时间大头」。

## 6. 本讲小结

- **结构化数据走结构化解析**:`json.load` 统一处理 train/prefill 的多行排版与 decode 的单行压缩;文本工具对单行 JSON 的计数会静默失真(实测 `decode.json` 的 kernel 计数:文本工具得 1,解析器得 3647)。
- **统计口径两条铁律**:时长只累计 `ph=="X"`;占比只在同一 `cat` 内部计算(CPU 墙钟 ≠ GPU 执行)。
- **三个聚合维度各司其职**:`cat` 看层次、名称看家族(需截断归一)、`pid`/`args.stream` 看设备与队列;kernel 事件的 `tid` 就是 stream。
- **聚合结果本身就是结构指纹**:decode 的 cpu_op 骤减暴露 CUDA Graph;train 的大内核榜(23 DeepEP + 4 FlashAttnBwd + 4 grouped GEMM,全部实测)正是 DualPipe 要重叠的三类大块。
- **实测锚点**:DeepEP 36 内核共 113.44ms ≈ 46.5%(train);dispatch_ll 235 次共 11.92ms、双峰 17~19/58~86µs(decode);kernel 总时长约 244ms(由 46.5% 反推,待本地验证)。

## 7. 下一步学习建议

- 下一讲 **u3-l2(通信-计算重叠率与气泡量化)** 会把本讲的 `dur` 升级成区间 \( [ts,\; ts+dur) \),对两条流的事件集合做交集/差集运算——请保留 `analyze_trace.py`,它将作为 u3-l2 的输入基座。
- 复习 u2-l1 的 correlation 机制:本讲的 `top_kernels` 只用了 GPU 侧信息,若想知道某个 Top 内核由哪个 CPU 算子启动,把 u2-l1 的反查逻辑嫁接进来即可(decode 例外:整条前向共享 correlation 7077)。
- 在 u3-l3 中,你会用本讲脚本对三份轨迹做横向对比,产出统一指标的对比表。
- 延伸阅读仓库内数据:`train.json` 尾部的元数据段([L97396 起](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97396))与 [README.md](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md) 中对三种场景的描述,是解释报表数字含义的最终依据。
